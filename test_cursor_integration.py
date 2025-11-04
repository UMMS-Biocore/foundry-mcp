#!/usr/bin/env python3
"""
Interactive Cursor Integration Test for ViaFoundry MCP
Run this to verify your setup before testing in Cursor.
"""

import os
import sys
import subprocess
from pathlib import Path

def print_header(text):
    """Print a formatted header."""
    print("\n" + "="*70)
    print(f"  {text}")
    print("="*70)

def print_success(text):
    """Print success message."""
    print(f"✅ {text}")

def print_error(text):
    """Print error message."""
    print(f"❌ {text}")

def print_info(text):
    """Print info message."""
    print(f"ℹ️  {text}")

def check_command_exists():
    """Check if viafoundry-mcp command is available."""
    print_header("1. Checking viafoundry-mcp command")
    
    try:
        result = subprocess.run(
            ['which', 'viafoundry-mcp'],
            capture_output=True,
            text=True,
            check=True
        )
        path = result.stdout.strip()
        print_success(f"Command found at: {path}")
        return True
    except subprocess.CalledProcessError:
        print_error("viafoundry-mcp command not found in PATH")
        print_info("Install with: pip install -e .")
        return False

def check_credentials():
    """Check if credentials file exists."""
    print_header("2. Checking credentials configuration")
    
    env_file = Path.home() / '.config' / 'viafoundry-mcp' / '.env'
    
    if not env_file.exists():
        print_error(f"Credentials file not found: {env_file}")
        print_info("Create it with:")
        print_info(f"  mkdir -p {env_file.parent}")
        print_info(f"  cat > {env_file} << EOF")
        print_info("  VIAFOUNDRY_HOSTNAME=https://your-instance.com")
        print_info("  VIAFOUNDRY_USERNAME=your-username")
        print_info("  VIAFOUNDRY_PASSWORD=your-password")
        print_info("  EOF")
        return False
    
    # Check file permissions
    stat_info = env_file.stat()
    mode = oct(stat_info.st_mode)[-3:]
    
    print_success(f"Credentials file exists: {env_file}")
    print_info(f"File permissions: {mode}")
    
    if mode != '600':
        print_info("Recommended permissions: 600 (owner read/write only)")
        print_info(f"Fix with: chmod 600 {env_file}")
    
    # Check if file has content
    try:
        with open(env_file, 'r') as f:
            content = f.read()
            
        if 'VIAFOUNDRY_HOSTNAME' in content:
            print_success("Contains VIAFOUNDRY_HOSTNAME")
        else:
            print_error("Missing VIAFOUNDRY_HOSTNAME")
            
        if 'VIAFOUNDRY_USERNAME' in content:
            print_success("Contains VIAFOUNDRY_USERNAME")
        else:
            print_error("Missing VIAFOUNDRY_USERNAME")
            
        if 'VIAFOUNDRY_PASSWORD' in content:
            print_success("Contains VIAFOUNDRY_PASSWORD")
        else:
            print_error("Missing VIAFOUNDRY_PASSWORD")
        
        return True
        
    except Exception as e:
        print_error(f"Error reading credentials: {e}")
        return False

def check_cursor_config():
    """Check Cursor MCP configuration."""
    print_header("3. Checking Cursor MCP configuration")
    
    mcp_config = Path.home() / '.cursor' / 'mcp.json'
    
    if not mcp_config.exists():
        print_error(f"Cursor MCP config not found: {mcp_config}")
        print_info("Create it with:")
        print_info('  {')
        print_info('    "mcpServers": {')
        print_info('      "viafoundry": {')
        print_info('        "command": "bash",')
        print_info('        "args": ["-l", "-c", "viafoundry-mcp"]')
        print_info('      }')
        print_info('    }')
        print_info('  }')
        return False
    
    print_success(f"Cursor config exists: {mcp_config}")
    
    # Check if viafoundry is configured
    try:
        import json
        with open(mcp_config, 'r') as f:
            config = json.load(f)
        
        if 'mcpServers' in config and 'viafoundry' in config['mcpServers']:
            print_success("ViaFoundry server is configured")
            
            server_config = config['mcpServers']['viafoundry']
            command = server_config.get('command')
            args = server_config.get('args', [])
            
            print_info(f"Command: {command}")
            print_info(f"Args: {' '.join(args) if args else '(none)'}")
            
            return True
        else:
            print_error("ViaFoundry server not found in config")
            return False
            
    except Exception as e:
        print_error(f"Error reading config: {e}")
        return False

def test_server_start():
    """Test if server can start."""
    print_header("4. Testing server startup")
    
    print_info("Starting viafoundry-mcp server (will auto-terminate)...")
    
    try:
        # Start server
        proc = subprocess.Popen(
            ['viafoundry-mcp'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        
        # Give it a moment to start
        import time
        time.sleep(1)
        
        # Check if it's still running
        if proc.poll() is None:
            print_success("Server started successfully")
            print_info("Server is waiting for MCP protocol messages (correct behavior)")
            
            # Clean up
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except:
                proc.kill()
            
            return True
        else:
            stdout, stderr = proc.communicate()
            print_error("Server exited unexpectedly")
            if stderr:
                print(f"Error output:\n{stderr}")
            return False
            
    except FileNotFoundError:
        print_error("viafoundry-mcp command not found")
        return False
    except Exception as e:
        print_error(f"Error starting server: {e}")
        return False

def print_next_steps():
    """Print next steps for testing in Cursor."""
    print_header("Next Steps: Test in Cursor")
    
    print("\n🎯 How to test in Cursor:")
    print("\n1. Restart Cursor completely:")
    print("   • Press Cmd+Q to quit (not just close windows)")
    print("   • Wait 5 seconds")
    print("   • Reopen Cursor")
    
    print("\n2. Open any chat and try these queries:")
    print('   • "What ViaFoundry tools do you have access to?"')
    print('   • "List all ViaFoundry pipelines"')
    print('   • "Search for datasets about RNA-seq in ViaFoundry"')
    
    print("\n3. Expected behavior:")
    print("   ✅ AI should list 12 ViaFoundry tools")
    print("   ✅ AI should use tools like 'list_all_processes'")
    print("   ✅ You'll see tool invocations in the chat")
    
    print("\n4. If tools don't appear:")
    print("   • Check Cursor logs:")
    print("     ls -lt ~/Library/Logs/Cursor/*/window*/exthost/*.log")
    print("   • Try MCP Inspector:")
    print("     npx @modelcontextprotocol/inspector viafoundry-mcp")
    print("   • See CURSOR_INTEGRATION_TEST.md for detailed troubleshooting")
    
    print("\n📚 Documentation:")
    print("   • README.md - Full setup guide")
    print("   • CURSOR_INTEGRATION_TEST.md - Comprehensive test guide")
    print("   • TESTING_GUIDE.md - Detailed testing instructions")

def print_tools_summary():
    """Print summary of available tools."""
    print_header("Available ViaFoundry MCP Tools (12 Total)")
    
    print("\n📊 Report Management (7 tools):")
    tools = [
        "fetch_report - Get complete report data",
        "list_processes - List processes in a report",
        "list_files - List files (all or by process)",
        "download_file - Download files from reports",
        "load_file - View file contents directly",
        "upload_file - Upload files to reports",
        "get_report_dirs - List available upload directories"
    ]
    for tool in tools:
        print(f"   • {tool}")
    
    print("\n🔬 Pipeline Management (2 tools):")
    tools = [
        "list_all_processes - List all ViaFoundry pipelines",
        "get_process_details - Get detailed pipeline information"
    ]
    for tool in tools:
        print(f"   • {tool}")
    
    print("\n🗂️ Metadata & Dataset Search (3 tools):")
    tools = [
        "search_datasets - Search for dataset files",
        "search_collections - Search for dataset collections",
        "get_collection_details - Get collection details"
    ]
    for tool in tools:
        print(f"   • {tool}")

def main():
    """Run all integration tests."""
    print("\n" + "🚀 ViaFoundry MCP - Cursor Integration Test".center(70))
    print("Testing your local setup before using in Cursor\n".center(70))
    
    # Run all checks
    checks = [
        check_command_exists(),
        check_credentials(),
        check_cursor_config(),
        test_server_start()
    ]
    
    # Summary
    print_header("Test Summary")
    
    passed = sum(checks)
    total = len(checks)
    
    print(f"\nPassed: {passed}/{total} checks")
    
    if passed == total:
        print_success("All checks passed! ✨")
        print_info("Your ViaFoundry MCP server is ready for Cursor integration")
        print_tools_summary()
        print_next_steps()
        return 0
    else:
        print_error(f"Some checks failed ({total - passed} failures)")
        print_info("Fix the issues above and run this test again")
        return 1

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\n❌ Test cancelled by user")
        sys.exit(1)

