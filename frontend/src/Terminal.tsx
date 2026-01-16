import { useEffect, useRef, useCallback, useState } from 'react';
import { Terminal as XTerm } from 'xterm';
import { FitAddon } from '@xterm/addon-fit';
import { WebLinksAddon } from '@xterm/addon-web-links';
import 'xterm/css/xterm.css';

interface TerminalProps {
  className?: string;
  userId: string;
}

export default function Terminal({ className, userId }: TerminalProps) {
  const terminalRef = useRef<HTMLDivElement>(null);
  const xtermRef = useRef<XTerm | null>(null);
  const fitAddonRef = useRef<FitAddon | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<number | null>(null);
  const [connected, setConnected] = useState(false);
  const [spriteName, setSpriteName] = useState<string | null>(null);
  const connectedToSpriteRef = useRef(false);

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const host = window.location.host;
    const ws = new WebSocket(`${protocol}//${host}/ws/terminal`);

    ws.onopen = () => {
      // Send connect message with user_id first
      console.log('[Terminal] Sending connect with userId:', userId);
      ws.send(JSON.stringify({ type: 'connect', user_id: userId }));
    };

    ws.onmessage = (event) => {
      // Check if it's a JSON control message
      if (event.data.startsWith('{')) {
        try {
          const msg = JSON.parse(event.data);

          if (msg.type === 'connected') {
            connectedToSpriteRef.current = true;
            setConnected(true);
            setSpriteName(msg.sprite_name || null);

            // Send initial size now that we're connected
            if (xtermRef.current) {
              const { cols, rows } = xtermRef.current;
              ws.send(JSON.stringify({ type: 'resize', cols, rows }));
            }
            return;
          }

          if (msg.type === 'error') {
            console.error('Terminal error:', msg.message);
            if (xtermRef.current) {
              xtermRef.current.write(`\r\n\x1b[31mError: ${msg.message}\x1b[0m\r\n`);
            }
            return;
          }
        } catch {
          // Not JSON, write as terminal output
        }
      }

      // Regular terminal output
      if (xtermRef.current) {
        xtermRef.current.write(event.data);
      }
    };

    ws.onclose = () => {
      setConnected(false);
      connectedToSpriteRef.current = false;
      wsRef.current = null;
      // Reconnect after 2 seconds
      reconnectTimeoutRef.current = window.setTimeout(connect, 2000);
    };

    ws.onerror = (e) => {
      console.error('Terminal WebSocket error:', e);
    };

    wsRef.current = ws;
  }, [userId]);

  useEffect(() => {
    if (!terminalRef.current) return;

    // Create terminal
    const xterm = new XTerm({
      cursorBlink: true,
      fontSize: 13,
      fontFamily: "'JetBrains Mono', monospace",
      theme: {
        background: '#0f1115',
        foreground: '#c9ccd1',
        cursor: '#f59e0b',
        cursorAccent: '#0f1115',
        selectionBackground: '#2a2f3a',
        black: '#1a1d23',
        red: '#ef4444',
        green: '#10b981',
        yellow: '#f59e0b',
        blue: '#3b82f6',
        magenta: '#8b5cf6',
        cyan: '#06b6d4',
        white: '#c9ccd1',
        brightBlack: '#6b7280',
        brightRed: '#f87171',
        brightGreen: '#34d399',
        brightYellow: '#fbbf24',
        brightBlue: '#60a5fa',
        brightMagenta: '#a78bfa',
        brightCyan: '#22d3ee',
        brightWhite: '#e5e7eb',
      },
    });

    // Add fit addon
    const fitAddon = new FitAddon();
    xterm.loadAddon(fitAddon);
    fitAddonRef.current = fitAddon;

    // Add web links addon
    const webLinksAddon = new WebLinksAddon();
    xterm.loadAddon(webLinksAddon);

    // Open terminal in container
    xterm.open(terminalRef.current);
    fitAddon.fit();

    xtermRef.current = xterm;

    // Handle input - only send if connected to sprite
    xterm.onData((data) => {
      if (wsRef.current?.readyState === WebSocket.OPEN && connectedToSpriteRef.current) {
        wsRef.current.send(data);
      }
    });

    // Handle resize - only send if connected to sprite
    xterm.onResize(({ cols, rows }) => {
      if (wsRef.current?.readyState === WebSocket.OPEN && connectedToSpriteRef.current) {
        wsRef.current.send(JSON.stringify({ type: 'resize', cols, rows }));
      }
    });

    // Connect to WebSocket
    connect();

    // Handle window resize
    const handleResize = () => {
      if (fitAddonRef.current) {
        fitAddonRef.current.fit();
      }
    };
    window.addEventListener('resize', handleResize);

    // Cleanup
    return () => {
      window.removeEventListener('resize', handleResize);
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
      }
      if (wsRef.current) {
        wsRef.current.close();
      }
      xterm.dispose();
    };
  }, [connect]);

  // Re-fit when visibility changes
  useEffect(() => {
    const handleVisibilityChange = () => {
      if (!document.hidden && fitAddonRef.current) {
        setTimeout(() => fitAddonRef.current?.fit(), 100);
      }
    };
    document.addEventListener('visibilitychange', handleVisibilityChange);
    return () => document.removeEventListener('visibilitychange', handleVisibilityChange);
  }, []);

  return (
    <div className={`terminal-container ${className || ''}`}>
      <div className="terminal-header">
        <span className="terminal-title">
          TERMINAL {spriteName && <span className="terminal-sprite-name">({spriteName})</span>}
        </span>
        <span
          className={`connection-dot ${connected ? 'connected' : 'disconnected'}`}
          title={connected ? `Connected to ${spriteName}` : 'Disconnected'}
        />
      </div>
      <div className="terminal-content" ref={terminalRef} />
    </div>
  );
}
