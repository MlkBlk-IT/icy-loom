"""IcyLoom — passerelle locale unifiée (cache LLM + tiroir de contexte).

Projet autonome : aucun import, aucun lien, aucun chemin vers un autre projet.
Tout est piloté par un unique fichier de configuration YAML.
"""

__all__ = ["__version__", "PROJECT_NAME", "register"]

__version__ = "2.2.1"
PROJECT_NAME = "icyloom"


def register(ctx) -> None:
    """Entry point for Hermes plugin system.
    
    Registers the IcyLoom MCP server tools with the Hermes agent.
    The actual MCP server runs independently on ports 8150-8153.
    This plugin declares the available tools so the LLM knows they exist.
    """
    # Tool schemas are defined in mcpserver.py and exposed via the MCP protocol.
    # Hermes will discover them via the MCP connection to the running IcyLoom server.
    # This register() is called when the plugin is loaded; it declares metadata.
    tool_names = [
        "cache",
        "ctx_admin",
        "ctx_check",
        "ctx_get",
        "ctx_profile",
        "ctx_session",
        "ctx_store",
        "ctx_verify",
        "gateway_stats",
        "invoke_tool",
        "ledger",
        "list_prompts",
        "list_resources",
        "proxy_info",
        "read_resource",
        "run_tool_script",
        "search_tools",
        "tool_cache_clear",
        "tool_drift",
        "tool_policy",
        "tool_simulate",
    ]
    for name in tool_names:
        ctx.register_tool(
            name=name,
            toolset="icyloom",
            schema={"type": "object", "description": f"IcyLoom tool: {name}"},
            handler=lambda *args, **kwargs: {"ok": False, "error": "not_connected", "message": "IcyLoom MCP server not running. Start with: icyloom serve"},
        )
