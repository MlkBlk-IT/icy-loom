# RESEARCH — Upgrades possibles pour le pipeline contexte IcyLoom

Date des sources : 2026-10-26 (recherche web du jour). Rien ici n'est implémenté. La nouveauté réelle de chaque piste n'est pas vérifiée (pas de revue exhaustive de prior art).

Cible produit : utilisateurs normaux et heavy-use sur modèles cloud. Le test LM Studio local sert à Malik pour valider, pas à définir le produit.

## 1. Paysage (déjà existant, donc pas un différenciateur seul)
| Projet | Ce qu'il fait | Source |
|---|---|---|
| Compresr Context Gateway (YC W26) | Proxy open source Go, compresse sorties d'outils + historique ; 500 K$ seed ; benchmark 141 questions/79 documents, ~106K → ~10,5K tokens, précision 72,3 → 74,5 % | https://www.respan.ai/market-map/compresr · https://neuronfeed.com/startups/compresr |
| agentgateway (compression) | Moteur de compression branchable ; écarte un résultat qui casse l'appariement appel/résultat d'outil | https://agentgateway.dev/blog/2026-07-27-optimize-token-cost-with-context-compression/ |
| Winnow | Local-first, réversible (pointeur + récupération), livraison selon la taille du modèle | https://glama.ai/mcp/servers/jpoindexter/winnow |
| ContextLedger | Réversible, -44 % tokens de sorties d'outils sur 127 payloads | https://dotzlaw.com/projects/contextledger-token-compression/ |
| LangChain Deep Agents | Offload vers filesystem + pointeurs, seuil 85 % du contexte | https://www.langchain.com/blog/context-management-for-deepagents |

## 2. Upgrades classés par impact
1. **Mesurer le coût facturé, pas les tokens estimés.** Une analyse de 2 848 runs facturés indique que plus la compression est agressive, pire le coût, car le préfixe change et le prompt cache est invalidé. Source : https://codex.danielvaughan.com/2026/07/28/token-reduction-is-not-cost-reduction-prompt-cache-economics-codex-cli-tool-output-compaction-billing/
   - Risque dans le design actuel (déduit, à vérifier) : les tours précédents passent de brut à compressé à chaque requête → préfixe modifié à chaque tour.
   - Test : lire `cached_tokens` dans les `usage` des fournisseurs.
2. **Compaction par lots, préfixe stable.** TokenPilot (arXiv 2606.17016) : -56 % à -87 % de coût, en stabilisant les préfixes et en évinçant par lots. Source : https://sotaverified.org/papers/260617016
3. **Optimisation de l'ancre par les échecs (ACON, arXiv 2510.00615).** Règles de compression mises à jour à partir de paires « complet réussit / compressé échoue » ; -26 à -54 % de tokens en pic ; distillable en petit compresseur (>95 % de précision conservée). Source : https://arxiv.org/abs/2510.00615v2
4. **Ancre indépendante de la requête.** La compression sensible à la requête produit un préfixe différent à chaque requête, incompatible avec le prompt caching. Toute réinjection selon la question doit aller en fin de requête, après le préfixe. Source : https://arxiv.org/pdf/2607.15516
5. **Masking comme mode de base.** Le masking simple divise le coût par deux en égalant le résumé LLM ; hybride -7/-11 % de plus. Le benchmark doit le battre. Source : https://arxiv.org/abs/2508.21433
6. **Livraison selon le modèle.** Pointeur pour modèles forts, texte inline plus large pour petits modèles. Source : Winnow (ci-dessus).
7. **Validateur + repli sur le brut.** Écarter tout résultat compressé qui casse le tool-calling ; mesurer la longueur de trajectoire (résumer a allongé les trajectoires de 13-15 % selon JetBrains Research, cité par agentgateway).

## 3. Cadre théorique
- Surveys 2026 : but = « l'état récupérable le plus court » (ACL ARR mai 2026) ; limites théoriques de la compression avec perte peu explorées (preprints.org 202607.0924).

## 4. Métriques du benchmark à construire
Coût facturé, taux de réussite, longueur de trajectoire, hit rate du cache fournisseur, survie du détail (needle) avec/sans compression.

## 5. Idées de Malik (2026-10-26)
### 5.1 Optimiser la forme, pas seulement compresser (sans perte de faits)
- Les tokens sont des morceaux de mots : le coût dépend de la forme du texte.
- Réécrire les chunks sous une forme moins chère en tokens : dédup avec compteur (`ligne ×47`), dictionnaire de préfixes/chemins défini une fois en en-tête, normalisation du bruit (ANSI, timestamps), JSON verbeux → table compacte.
- Principe (inférence, à tester) : le rare est important. Les lignes répétées s'effondrent, la ligne FATAL unique survit → vise le needle perdu (E7778).
- Mesurer avec le tokenizer réel du fournisseur ; n'accepter la réécriture que si les tokens baissent. Déterministe donc compatible avec le cache.

### 5.2 Vecteurs envoyés au LLM
- Seulement possible si le serveur accepte des embeddings : vLLM `prompt_embeds` (serveur contrôlé, poids ouverts) https://docs.vllm.ai/en/stable/features/prompt_embeds/
- Aucune preuve trouvée que Gemini/OpenAI/Anthropic acceptent des vecteurs (non vérifié). Un vecteur est lié à un modèle et ne restitue pas le texte exact. → Réservé à un futur mode self-hosted.

### 5.3 Protocole « paquet » (déjà présent, à renforcer)
- En-tête = marqueur/ancre, charge utile = chunks stockés, réassemblage = réhydratation `ctx_get`.
- À ajouter : somme de contrôle à la réhydratation, numéros de séquence, retransmission automatique + avertissement si un chunk manque.

### 5.4 Base de données « sens pour sens »
- Mem0 : extrait des faits atomiques, les indexe en vecteurs ; ~7 000 tokens par récupération contre 25 000+ en contexte complet, précision comparable (chiffres du vendeur). https://mem0.ai/research
- Limites : benchmarks contestés par Letta et Zep ; ~15 % d'extractions perdent ou paraphrasent un fait ; chaque écriture coûte un appel LLM. https://rywalker.com/research/mem0 · https://www.emergentmind.com/topics/mem0
- Conséquence pour IcyLoom : une paraphrase peut perdre un identifiant exact. Design hybride proposé (inférence) : la DB garde le texte exact (vérité) + entités typées extraites de façon déterministe (codes d'erreur, chemins, IDs, nombres) + résumés sémantiques optionnels. Le sens sert à retrouver, l'exact sert à répondre.
- Les éléments récupérés vont en fin de requête (après le préfixe) pour ne pas casser le cache.

## 6. Appels d'outils répétés (boucle)
### 6.1 Causes
- La description de l'outil ne dit pas quand arrêter, et le modèle raisonne sur une image compressée qui cache la répétition. https://dev.to/gabrielanhaia/why-your-langchain-agent-keeps-calling-the-same-tool-in-a-loop-and-how-to-stop-it-57gk
- Donc la compression peut elle-même provoquer les rappels d'outils.

### 6.2 Contre-mesures au niveau gateway (à tester)
1. **Registre d'appels** : empreinte = hash(nom d'outil + arguments normalisés) ; fenêtre glissante des 20 derniers appels ; détection en 3 répétitions (méthode d'un agent terminal, arXiv 2603.05344). https://arxiv.org/pdf/2603.05344
2. **Mémoïsation au lieu de blocage** : sur appel identique, servir le résultat stocké en DB. Références : tool-call-cache (12 appels HTTP identiques observés) https://dev.to/mukundakatta/stop-paying-for-the-same-tool-call-twice-memoize-llm-tool-calls-with-tool-call-cache-56kp ; ToolCaching arXiv 2601.15335 https://arxiv.org/abs/2601.15335 ; coalescence des appels concurrents + cache exact/sémantique (toolfusion) https://pypi.org/project/toolfusion/ ; AWS : cache à deux couches session + inter-sessions https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentcost04-bp03.md
3. **Indice dans l'ancre** (inférence) : l'ancre inclut « outil X(args) déjà appelé au tour N, résultat stocké (id), faits clés ». Le modèle voit qu'il a déjà l'info → évite le rappel à la source.
4. **Garde de boucle à paliers** avec signaux de progrès (arguments normalisés répétés, résultats inchangés, état de tâche inchangé) et distinction retry fournisseur / boucle réelle. https://oneuptime.com/blog/post/2026-09-12-detect-tool-loops-dead-ends-production-agents/markdown ; seuils type OpenClaw (répétition 3, critique 6) https://docs.openclaw.ai/tools/loop-detection
5. **Ne pas mettre en cache** : outils à effets de bord, outils de polling, outils dont les mêmes arguments donnent volontairement des résultats différents ; outils à état → clé liée à l'état (TVCache arXiv 2602.10986). https://arxiv.org/html/2602.10986v1
6. **Sémantique** : équivalence sémantique des appels = problème de recherche ; commencer par l'exact, sémantique en option par outil.
- IcyCache expose déjà un outil `check_tool_loop` (vu dans la liste d'outils ; comportement non lu).

## 7. Priorité proposée (inférence)
1. Mesure du coût facturé + test de stabilité du préfixe.
2. Forme optimisée sans perte (5.1).
3. Registre d'appels + mémoïsation + indice dans l'ancre (6.2).
4. DB hybride exact + sens (5.4).
5. Optimisation de l'ancre par les échecs (ACON, section 2).

## 8. À vérifier avant tout code
- Effet réel du changement brut → compressé sur le cache fournisseur (Gemini/OpenAI/Anthropic).
- Si la réinjection en fin de requête règle le needle perdu.
- Nouveauté réelle de la réhydratation côté gateway.
