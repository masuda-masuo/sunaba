import asyncio
from sunaba import server

tools = asyncio.run(server.mcp.list_tools())

desc_sizes = {t.name: len((t.description or '').encode('utf-8')) for t in tools}
param_sizes = {}
for t in tools:
    props = (t.parameters or {}).get('properties') or {}
    param_sizes[t.name] = sum(len((p.get('description') or '').encode('utf-8')) for p in props.values())

print('Description total:', sum(desc_sizes.values()))
print('Param total:', sum(param_sizes.values()))
print('edit_file desc:', desc_sizes.get('edit_file'))
print('edit_file param:', param_sizes.get('edit_file'))
