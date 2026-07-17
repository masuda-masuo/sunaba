import asyncio
from sunaba import server

tools = asyncio.run(server.mcp.list_tools())
for t in tools:
    if t.name == 'edit_file':
        b = len((t.description or '').encode('utf-8'))
        print(f'edit_file description: {b} bytes (limit 2048)')
        print('---description start---')
        print(t.description)
        print('---description end---')
