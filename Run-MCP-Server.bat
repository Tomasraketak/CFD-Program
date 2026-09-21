@echo off
rem  AeroThermalStudio - MCP server for AI agents.
rem
rem  Normally started by the AI client rather than by hand. Point the client
rem  at this file; see docs\en\MCP_AI_GUIDE.md for the configuration.
rem
rem  The server talks JSON-RPC over stdin/stdout, so this window will look
rem  idle. That is correct - do not close it while the agent is working.
title AeroThermalStudio - MCP Server
set "ATS_MODE=--mcp"
set "ATS_ARGS=%*"
call "%~dp0_launcher.cmd"
