
import json, sys, importlib.util
spec = importlib.util.spec_from_file_location("faba_server", r"C:\VectraFi\mcp\faba_server.py")
mod  = importlib.util.module_from_spec(spec)
sys.modules["faba_server"] = mod
spec.loader.exec_module(mod)
tools = mod.mcp._tool_manager.list_tools()
out = [{
    "name": t.name,
    "description": bool(t.description),
    "params": list((t.parameters or {}).get("properties", {}).keys()),
} for t in tools]
print(json.dumps(out))
