# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Monios is a full-stack AI chat application with an integrated development environment. It features a FastAPI backend with a React frontend, WebSocket-based real-time communication, file system monitoring, terminal access, and code viewing capabilities.

## Build & Development Commands

### Frontend (in `frontend/` directory)
```bash
bun run dev      # Development server on localhost:5173 (proxies to backend)
bun run build    # Production build to dist/
bun run preview  # Preview production build
```

### Backend (from root)
```bash
pip install -r requirements.txt  # Install dependencies
python main.py                    # Development server on localhost:8000 (auto-reload)
uvicorn main:app --host 0.0.0.0 --port 8000  # Production
```

### Environment Variables
- `DEV_MODE=1` - Enable development auth bypass (skips JWT validation)
- `MODAL_ENVIRONMENT` - Deploy mode selection (local vs Modal serverless)

## Architecture

### Backend (Python/FastAPI)
- **main.py** - FastAPI app entry point, WebSocket endpoints (`/ws/chat`, `/ws/files`, `/ws/terminal`)
- **sessions.py** - Per-user async message queue system with position tracking and cancel actions
- **file_manager.py** - Watchdog-based file system monitoring, broadcasts events to WebSocket clients
- **terminal.py** - PTY wrapper for shell access, working directory is `/workspace`
- **auth/** - Google OAuth 2.0 + JWT authentication with DEV_MODE bypass
- **routes/** - REST API handlers for auth, chat, and file operations

### Frontend (React 19/TypeScript/Vite)
- **App.tsx** - Main component orchestrating chat, terminal, and file explorer panels
- **AuthContext.tsx** - Google OAuth context provider with guest mode support
- **FileExplorer.tsx** - File browser with drag-drop to insert paths into chat
- **Terminal.tsx** - XTerm.js wrapper connected via WebSocket
- **CodeViewer.tsx** - Syntax-highlighted file content viewer

### Key Data Flows
1. **Chat**: WebSocket → message queue (per-user) → Claude SDK → streamed response
2. **Files**: Watchdog events → FileEvent dataclass → broadcast to `/ws/files` clients
3. **Terminal**: WebSocket → PTY stdin/stdout, resize messages for terminal dimensions

### WebSocket Message Formats
- **Terminal resize**: `{"type": "resize", "cols": N, "rows": N}`
- **Chat messages**: Queued with unique IDs, status transitions (sending→queued→processing→done/error)
- **File events**: Real-time tree updates on file system changes

## Tech Stack

**Backend**: FastAPI, Uvicorn, claude-agent-sdk, watchdog, python-jose (JWT), google-auth
**Frontend**: React 19, TypeScript, Vite, Bun, XTerm.js, highlight.js
**Styling**: CSS custom properties with dark/light theme toggle, JetBrains Mono font
