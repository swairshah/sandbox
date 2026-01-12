import { useState, useEffect, useRef, useCallback } from 'react';
import hljs from 'highlight.js';
// Import a dark theme that matches our app aesthetic
import 'highlight.js/styles/atom-one-dark.css';

interface FileData {
  path: string;
  name: string;
  content: string | null;
  size: number;
  truncated: boolean;
  is_binary: boolean;
  extension: string;
}

interface CodeViewerProps {
  filePath: string | null;
  readOnly?: boolean;
  onClose?: () => void;
}

// Map file extensions to highlight.js language names
const extensionToLanguage: Record<string, string> = {
  '.ts': 'typescript',
  '.tsx': 'typescript',
  '.js': 'javascript',
  '.jsx': 'javascript',
  '.py': 'python',
  '.rs': 'rust',
  '.go': 'go',
  '.java': 'java',
  '.cpp': 'cpp',
  '.c': 'c',
  '.h': 'c',
  '.hpp': 'cpp',
  '.cs': 'csharp',
  '.rb': 'ruby',
  '.php': 'php',
  '.swift': 'swift',
  '.kt': 'kotlin',
  '.scala': 'scala',
  '.html': 'html',
  '.htm': 'html',
  '.css': 'css',
  '.scss': 'scss',
  '.sass': 'scss',
  '.less': 'less',
  '.json': 'json',
  '.yaml': 'yaml',
  '.yml': 'yaml',
  '.toml': 'toml',
  '.xml': 'xml',
  '.md': 'markdown',
  '.sh': 'bash',
  '.bash': 'bash',
  '.zsh': 'bash',
  '.sql': 'sql',
  '.dockerfile': 'dockerfile',
  '.makefile': 'makefile',
  '.lua': 'lua',
  '.r': 'r',
  '.perl': 'perl',
  '.pl': 'perl',
  '.vim': 'vim',
  '.ini': 'ini',
  '.conf': 'ini',
  '.env': 'ini',
  '.gitignore': 'plaintext',
  '.txt': 'plaintext',
};

function getLanguage(filename: string, extension: string): string {
  // Check for special filenames
  const lowerName = filename.toLowerCase();
  if (lowerName === 'dockerfile') return 'dockerfile';
  if (lowerName === 'makefile') return 'makefile';
  if (lowerName.endsWith('.d.ts')) return 'typescript';

  return extensionToLanguage[extension.toLowerCase()] || 'plaintext';
}

export default function CodeViewer({ filePath, readOnly = true, onClose }: CodeViewerProps) {
  const [fileData, setFileData] = useState<FileData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const codeRef = useRef<HTMLElement>(null);

  const loadFile = useCallback(async (path: string) => {
    setLoading(true);
    setError(null);

    try {
      const response = await fetch(`/api/files/read?path=${encodeURIComponent(path)}`);

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({ detail: 'Failed to load file' }));
        throw new Error(errorData.detail || `HTTP ${response.status}`);
      }

      const data: FileData = await response.json();
      setFileData(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load file');
      setFileData(null);
    } finally {
      setLoading(false);
    }
  }, []);

  // Load file when path changes
  useEffect(() => {
    if (filePath) {
      loadFile(filePath);
    } else {
      setFileData(null);
      setError(null);
    }
  }, [filePath, loadFile]);

  // Apply syntax highlighting after content loads
  useEffect(() => {
    if (fileData?.content && codeRef.current) {
      // Reset any previous highlighting
      codeRef.current.removeAttribute('data-highlighted');
      hljs.highlightElement(codeRef.current);
    }
  }, [fileData?.content]);

  if (!filePath) {
    return (
      <div className="code-viewer">
        <div className="code-viewer-header">
          <span className="code-viewer-title">VIEWER</span>
          <div className="code-viewer-actions">
            {readOnly && <span className="read-only-badge">read-only</span>}
          </div>
        </div>
        <div className="code-viewer-empty">
          <div className="empty-icon">&#128196;</div>
          <div>Select a file from the explorer to view</div>
        </div>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="code-viewer">
        <div className="code-viewer-header">
          <span className="code-viewer-title">VIEWER</span>
        </div>
        <div className="code-viewer-loading">Loading...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="code-viewer">
        <div className="code-viewer-header">
          <span className="code-viewer-title">VIEWER</span>
          {onClose && (
            <button className="code-viewer-close" onClick={onClose} title="Close">
              &times;
            </button>
          )}
        </div>
        <div className="code-viewer-error">{error}</div>
      </div>
    );
  }

  if (!fileData) {
    return null;
  }

  const language = getLanguage(fileData.name, fileData.extension);

  return (
    <div className="code-viewer">
      <div className="code-viewer-header">
        <div className="code-viewer-file-info">
          <span className="code-viewer-filename" title={fileData.path}>
            {fileData.name}
          </span>
          <span className="code-viewer-meta">
            {language !== 'plaintext' && <span className="code-viewer-language">{language}</span>}
            <span className="code-viewer-size">{formatFileSize(fileData.size)}</span>
            {fileData.truncated && <span className="code-viewer-truncated">truncated</span>}
          </span>
        </div>
        <div className="code-viewer-actions">
          {readOnly && <span className="read-only-badge">read-only</span>}
          {onClose && (
            <button className="code-viewer-close" onClick={onClose} title="Close">
              &times;
            </button>
          )}
        </div>
      </div>

      <div className="code-viewer-content">
        {fileData.is_binary ? (
          <div className="code-viewer-binary">
            <div className="binary-icon">&#128230;</div>
            <div>Binary file ({fileData.extension || 'unknown type'})</div>
            <div className="binary-size">{formatFileSize(fileData.size)}</div>
          </div>
        ) : (
          <pre className="code-viewer-pre">
            <code
              ref={codeRef}
              className={`language-${language}`}
            >
              {fileData.content || ''}
            </code>
          </pre>
        )}
      </div>
    </div>
  );
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
