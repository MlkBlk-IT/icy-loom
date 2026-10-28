"""Pool de clients HTTP résilient.

Problème résolu : après une erreur fatale côté amont (400/401 en amont d'un
proxy mal configuré), une connexion httpx peut rester dans un état poisons.
La stratégie est **drain → recycle → retry** :

1. la requête est tentée sur le client courant du pool ;
2. en cas d'erreur *transitoire* (connexion réinitialisée, timeout de
   lecture, erreur de protocole), le client est **vidé** puis **recréé** ;
3. la requête est rejouée **une seule fois** sur le client neuf.

Un 4xx métier (400, 401, 404…) n'est **pas** transitoire : il est renvoyé
tel quel, sans recycler le client — on ne veut pas masquer une erreur de
requête par une boucle de retry.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger("icyloom.pool")

#: Codes qui Justifient un recyclage du client + un nouvel essai.
TRANSIENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class ClientPool:
    """Un client httpx par URL de base, recréé à la demande."""

    def __init__(self, timeout: float = 120.0, verify: bool = True) -> None:
        self._timeout = timeout
        self._verify = verify
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._lock = asyncio.Lock()

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout, connect=min(10.0, self._timeout)),
            follow_redirects=True,
            verify=self._verify,
        )

    async def client_for(self, base_url: str) -> httpx.AsyncClient:
        """Client associé à une URL de base (créé à la demande)."""
        async with self._lock:
            client = self._clients.get(base_url)
            if client is None or client.is_closed:
                client = self._new_client()
                self._clients[base_url] = client
            return client

    async def _recycle(self, base_url: str, client: httpx.AsyncClient) -> httpx.AsyncClient:
        """Remplace un client fatally|urlé par un neuf."""
        async with self._lock:
            if self._clients.get(base_url) is client:
                self._clients.pop(base_url, None)
        try:
            await client.aclose()
        except Exception:  # pragma: no cover - fermeture best-effort
            pass
        async with self._lock:
            fresh = self._clients.get(base_url)
            if fresh is None or fresh.is_closed:
                fresh = self._new_client()
                self._clients[base_url] = fresh
            return fresh

    async def request(
        self,
        method: str,
        url: str,
        base_key: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Envoie une requête ; recycle et rejoue une fois si c'est transitoire."""
        client = await self.client_for(base_key)
        try:
            response = await client.request(method, url, **kwargs)
        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError,
                httpx.ReadTimeout, httpx.PoolTimeout, httpx.WriteError):
            log.warning("erreur transport sur %s : recyclage du client", url)
            client = await self._recycle(base_key, client)
            response = await client.request(method, url, **kwargs)
        if response.status_code in TRANSIENT_STATUS:
            log.info("réponse transitoire %s sur %s : recyclage et nouvel essai",
                     response.status_code, url)
            client = await self._recycle(base_key, client)
            response = await client.request(method, url, **kwargs)
        return response

    async def aclose(self) -> None:
        async with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            try:
                await client.aclose()
            except Exception:  # pragma: no cover
                pass
