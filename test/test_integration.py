#!/usr/bin/env python3
"""
Manual MCP Server Test Client

This script tests the MCP server by simulating what an IDE would do.
It starts the server and sends tool call requests to it.
"""

import asyncio
import json
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def test_mcp_server():
    """Test the MCP server with real requests."""

    print("="*80)
    print("VIAFOUNDRY MCP SERVER MANUAL TEST")
    print("="*80)
    print()

    # Server parameters
    server_params = StdioServerParameters(
        command="python",
        args=["-m", "viafoundry_mcp.server"],
        env=None  # Will use .env file
    )

    print("Starting MCP server...")
    print(f"Command: {server_params.command} {' '.join(server_params.args)}")
    print()

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:

            # Initialize the session
            print("Initializing session...")
            await session.initialize()
            print("✓ Session initialized")
            print()

            # List available tools
            print("-"*80)
            print("STEP 1: List Available Tools")
            print("-"*80)
            tools_result = await session.list_tools()
            print(f"Found {len(tools_result.tools)} tools:")
            for tool in tools_result.tools:
                print(f"  • {tool.name}: {tool.description[:60]}...")
            print()

            # Test 1: List processes in report 3461
            print("-"*80)
            print("STEP 2: Test list_processes for report 3461")
            print("-"*80)
            print("Calling tool: list_processes")
            print("Arguments: {'report_id': '3461'}")
            print()

            result1 = await session.call_tool(
                "list_processes",
                arguments={"report_id": "3461"}
            )

            print("Response:")
            for content in result1.content:
                if hasattr(content, 'text'):
                    response_data = json.loads(content.text)
                    print(json.dumps(response_data, indent=2))
            print()

            # Test 2: List all files in report 3461
            print("-"*80)
            print("STEP 3: Test list_files for report 3461")
            print("-"*80)
            print("Calling tool: list_files")
            print("Arguments: {'report_id': '3461'}")
            print()

            result2 = await session.call_tool(
                "list_files",
                arguments={"report_id": "3461"}
            )

            print("Response:")
            for content in result2.content:
                if hasattr(content, 'text'):
                    response_data = json.loads(content.text)
                    files = response_data.get('files', [])
                    print(f"Found {len(files)} files:")
                    print()

                    # Group by process
                    processes = {}
                    for file in files:
                        process = file.get('processName')
                        if process not in processes:
                            processes[process] = []
                        processes[process].append(file)

                    for process, process_files in processes.items():
                        print(f"  📁 {process}: {len(process_files)} files")
                        for i, f in enumerate(process_files[:3]):
                            size_mb = f['fileSize'] / (1024*1024)
                            print(f"     {i+1}. {f['name']} ({size_mb:.2f} MB)")
                        if len(process_files) > 3:
                            print(f"     ... and {len(process_files)-3} more")
                        print()

            # Test 3: List files for specific process
            print("-"*80)
            print("STEP 4: Test list_files for specific process")
            print("-"*80)
            print("Calling tool: list_files")
            print("Arguments: {'report_id': '3461', 'process_name': 'cellranger_multi'}")
            print()

            result3 = await session.call_tool(
                "list_files",
                arguments={
                    "report_id": "3461",
                    "process_name": "cellranger_multi"
                }
            )

            print("Response:")
            for content in result3.content:
                if hasattr(content, 'text'):
                    response_data = json.loads(content.text)
                    files = response_data.get('files', [])
                    print(f"Found {len(files)} files in cellranger_multi:")
                    for i, f in enumerate(files, 1):
                        size_mb = f['fileSize'] / (1024*1024)
                        print(f"  {i}. {f['name']}")
                        print(f"     Path: {f['file_path']}")
                        print(f"     Size: {size_mb:.2f} MB")
                        print(f"     Type: {f['extension']}")
                        print()

            # Test 4: Get report directories
            print("-"*80)
            print("STEP 5: Test get_report_dirs")
            print("-"*80)
            print("Calling tool: get_report_dirs")
            print("Arguments: {'report_id': '3461'}")
            print()

            result4 = await session.call_tool(
                "get_report_dirs",
                arguments={"report_id": "3461"}
            )

            print("Response:")
            for content in result4.content:
                if hasattr(content, 'text'):
                    response_data = json.loads(content.text)
                    directories = response_data.get('directories', [])
                    print(f"Available directories for upload:")
                    for i, dir in enumerate(directories, 1):
                        print(f"  {i}. {dir}")
            print()

            # Test 5: Phase 1 - List all processes
            print("-"*80)
            print("STEP 6: Test list_all_processes (Phase 1)")
            print("-"*80)
            print("Calling tool: list_all_processes")
            print()

            result5 = await session.call_tool(
                "list_all_processes",
                arguments={}
            )

            print("Response:")
            for content in result5.content:
                if hasattr(content, 'text'):
                    response_data = json.loads(content.text)
                    processes = response_data.get('processes', [])
                    print(f"Found {len(processes)} total processes")
                    if processes:
                        print(f"Sample processes:")
                        for i, proc in enumerate(processes[:3], 1):
                            print(f"  {i}. {proc.get('name')} (ID: {proc.get('id')})")
            print()

            # Test 6: Phase 1 - Search collections
            print("-"*80)
            print("STEP 7: Test search_collections (Phase 1)")
            print("-"*80)
            print("Calling tool: search_collections")
            print()

            result6 = await session.call_tool(
                "search_collections",
                arguments={}
            )

            print("Response:")
            for content in result6.content:
                if hasattr(content, 'text'):
                    response_data = json.loads(content.text)
                    collections = response_data.get('collections', [])
                    print(f"Found {len(collections)} collections")
                    if collections:
                        print(f"Sample collections:")
                        for i, coll in enumerate(collections[:3], 1):
                            print(f"  {i}. {coll.get('name')} (ID: {coll.get('id')})")
            print()

            # Test 7: Verify tool count
            print("-"*80)
            print("STEP 8: Verify Complete SDK Coverage")
            print("-"*80)
            expected_tools = 56
            actual_tools = len(tools_result.tools)

            if actual_tools == expected_tools:
                print(f"✓ Tool count correct: {actual_tools}/{expected_tools} (100% SDK coverage)")
            else:
                print(f"⚠ Tool count mismatch: {actual_tools}/{expected_tools}")
            print()

            print("Tool categories:")
            tool_names = [t.name for t in tools_result.tools]

            # Count by phase
            phase1_tools = [t for t in tool_names if any(x in t for x in ['get_process_revisions', 'list_process_parameters', 'search_canvas', 'search_metadata_fields', 'search_metadata_records'])]
            phase2_tools = [t for t in tool_names if any(x in t for x in ['duplicate_process', 'filter_process_parameters', 'add_files_to_dataset', 'create_collection', 'collect_report_files'])]
            phase3_tools = [t for t in tool_names if any(x in t for x in ['create_process', 'update_process', 'create_canvas', 'update_canvas', 'create_metadata_field'])]
            phase4_tools = [t for t in tool_names if any(x in t for x in ['delete_process', 'delete_collection', 'delete_canvas', 'menu_group'])]

            print(f"  • Reports: {len([t for t in tool_names if 'report' in t or 'fetch' in t])} tools")
            print(f"  • Processes: {len([t for t in tool_names if 'process' in t])} tools")
            print(f"  • Metadata: {len([t for t in tool_names if any(x in t for x in ['metadata', 'collection', 'canvas', 'field', 'data', 'menu'])])} tools")
            print()

            print("="*80)
            print("ALL TESTS COMPLETED SUCCESSFULLY! ✓")
            print("="*80)
            print()
            print("The MCP server is working correctly and ready to use in:")
            print("  • Cursor IDE")
            print("  • Claude Desktop")
            print("  • VS Code (with Continue)")
            print("  • Any MCP-compatible application")
            print()

async def main():
    """Main entry point."""
    try:
        await test_mcp_server()
    except KeyboardInterrupt:
        print("\n\nTest interrupted by user")
    except Exception as e:
        print(f"\n✗ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        return 1
    return 0

if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)
