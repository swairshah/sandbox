import { useState, useEffect, useCallback, useRef } from 'react';

interface FileNode {
  name: string;
  path: string;
  type: 'file' | 'directory';
  children?: FileNode[];
}

interface FileEvent {
  event_type: 'created' | 'deleted' | 'modified' | 'moved';
  path: string;
  is_directory: boolean;
  dest_path?: string;
}

interface FileExplorerProps {
  onFileDragStart?: (path: string) => void;
  onFileSelect?: (path: string, isDirectory: boolean) => void;
}

// File type icons (using unicode/emoji for simplicity, can be replaced with SVG)
const getFileIcon = (name: string, type: 'file' | 'directory', isExpanded: boolean): string => {
  if (type === 'directory') {
    return isExpanded ? '\u25BC' : '\u25B6';
  }

  const ext = name.split('.').pop()?.toLowerCase() || '';
  const iconMap: Record<string, string> = {
    // Code files
    'ts': '\u{1F4D8}',
    'tsx': '\u{1F4D8}',
    'js': '\u{1F4D9}',
    'jsx': '\u{1F4D9}',
    'py': '\u{1F40D}',
    'rs': '\u{1F980}',
    'go': '\u{1F4A0}',
    'java': '\u2615',
    'cpp': '\u{1F4BB}',
    'c': '\u{1F4BB}',
    'h': '\u{1F4BB}',
    // Config
    'json': '\u{1F4CB}',
    'yaml': '\u{1F4CB}',
    'yml': '\u{1F4CB}',
    'toml': '\u{1F4CB}',
    'xml': '\u{1F4CB}',
    // Docs
    'md': '\u{1F4DD}',
    'txt': '\u{1F4C4}',
    'pdf': '\u{1F4D5}',
    // Web
    'html': '\u{1F310}',
    'css': '\u{1F3A8}',
    'scss': '\u{1F3A8}',
    // Images
    'png': '\u{1F5BC}',
    'jpg': '\u{1F5BC}',
    'jpeg': '\u{1F5BC}',
    'gif': '\u{1F5BC}',
    'svg': '\u{1F5BC}',
    // Data
    'sql': '\u{1F5C3}',
    'db': '\u{1F5C3}',
    // Shell
    'sh': '\u{1F4DF}',
    'bash': '\u{1F4DF}',
    'zsh': '\u{1F4DF}',
  };

  return iconMap[ext] || '\u{1F4C4}';
};

interface TreeNodeProps {
  node: FileNode;
  depth: number;
  expandedPaths: Set<string>;
  onToggle: (path: string) => void;
  onDragStart: (e: React.DragEvent, path: string) => void;
  onFileSelect?: (path: string, isDirectory: boolean) => void;
}

function TreeNode({ node, depth, expandedPaths, onToggle, onDragStart, onFileSelect }: TreeNodeProps) {
  const isExpanded = expandedPaths.has(node.path);
  const isDirectory = node.type === 'directory';
  const icon = getFileIcon(node.name, node.type, isExpanded);

  const handleClick = () => {
    if (isDirectory) {
      onToggle(node.path);
    } else {
      // File clicked - notify parent
      onFileSelect?.(node.path, false);
    }
  };

  const handleDragStart = (e: React.DragEvent) => {
    e.dataTransfer.setData('text/plain', node.path);
    e.dataTransfer.effectAllowed = 'copy';
    onDragStart(e, node.path);
  };

  return (
    <div className="tree-node">
      <div
        className={`tree-item ${isDirectory ? 'directory' : 'file'}`}
        style={{ paddingLeft: `${depth * 12 + 8}px` }}
        onClick={handleClick}
        draggable
        onDragStart={handleDragStart}
        title={node.path}
      >
        <span className={`tree-icon ${isDirectory ? 'folder-icon' : 'file-icon'}`}>
          {icon}
        </span>
        <span className="tree-name">{node.name}</span>
      </div>
      {isDirectory && isExpanded && node.children && (
        <div className="tree-children">
          {node.children.map((child) => (
            <TreeNode
              key={child.path}
              node={child}
              depth={depth + 1}
              expandedPaths={expandedPaths}
              onToggle={onToggle}
              onDragStart={onDragStart}
              onFileSelect={onFileSelect}
            />
          ))}
        </div>
      )}
    </div>
  );
}

export default function FileExplorer({ onFileDragStart, onFileSelect }: FileExplorerProps) {
  const [tree, setTree] = useState<FileNode | null>(null);
  const [expandedPaths, setExpandedPaths] = useState<Set<string>>(new Set(['.']));
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<number | null>(null);

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const host = window.location.host;
    const ws = new WebSocket(`${protocol}//${host}/ws/files`);

    ws.onopen = () => {
      setConnected(true);
      setError(null);
      ws.send(JSON.stringify({ type: 'subscribe' }));
    };

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);

        if (data.type === 'tree') {
          setTree(data.data);
        } else if (data.type === 'file_event') {
          handleFileEvent(data as FileEvent);
        } else if (data.type === 'error') {
          console.error('File WebSocket error:', data.error);
        }
      } catch (e) {
        console.error('Failed to parse WebSocket message:', e);
      }
    };

    ws.onclose = () => {
      setConnected(false);
      wsRef.current = null;
      // Reconnect after 2 seconds
      reconnectTimeoutRef.current = window.setTimeout(connect, 2000);
    };

    ws.onerror = (e) => {
      console.error('File WebSocket error:', e);
      setError('Connection error');
    };

    wsRef.current = ws;
  }, []);

  const handleFileEvent = (event: FileEvent) => {
    // Request fresh tree on any file event
    // This is simpler than trying to update the tree in place
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'get_tree', path: '' }));
    }
  };

  useEffect(() => {
    connect();

    return () => {
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
      }
      if (wsRef.current) {
        wsRef.current.close();
      }
    };
  }, [connect]);

  const handleToggle = (path: string) => {
    setExpandedPaths((prev) => {
      const next = new Set(prev);
      if (next.has(path)) {
        next.delete(path);
      } else {
        next.add(path);
      }
      return next;
    });
  };

  const handleDragStart = (_e: React.DragEvent, path: string) => {
    onFileDragStart?.(path);
  };

  const handleRefresh = () => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'get_tree', path: '' }));
    }
  };

  return (
    <div className="file-explorer">
      <div className="file-explorer-header">
        <span className="file-explorer-title">WORKSPACE</span>
        <div className="file-explorer-actions">
          <button
            className="file-explorer-action"
            onClick={handleRefresh}
            title="Refresh"
          >
            ↻
          </button>
          <span
            className={`connection-dot ${connected ? 'connected' : 'disconnected'}`}
            title={connected ? 'Connected' : 'Disconnected'}
          />
        </div>
      </div>
      <div className="file-explorer-content">
        {error && <div className="file-explorer-error">{error}</div>}
        {!tree && !error && <div className="file-explorer-loading">Loading...</div>}
        {tree && (
          <div className="file-tree">
            {tree.children && tree.children.length > 0 ? (
              tree.children.map((child) => (
                <TreeNode
                  key={child.path}
                  node={child}
                  depth={0}
                  expandedPaths={expandedPaths}
                  onToggle={handleToggle}
                  onDragStart={handleDragStart}
                  onFileSelect={onFileSelect}
                />
              ))
            ) : (
              <div className="file-explorer-empty">No files in workspace</div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
