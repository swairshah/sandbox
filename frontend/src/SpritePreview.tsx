import { useState, useRef } from 'react';

interface SpritePreviewProps {
  userId: string;
  className?: string;
}

export function SpritePreview({ userId, className = '' }: SpritePreviewProps) {
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [isExpanded, setIsExpanded] = useState(false);
  const iframeRef = useRef<HTMLIFrameElement>(null);

  // Proxy URL - goes through backend which adds auth
  // The sprite URL maps to port 8080 inside the sprite by default
  const previewUrl = `/api/preview/${userId}/`;

  const handleLoad = () => {
    setIsLoading(false);
    setError(null);
  };

  const handleError = () => {
    setIsLoading(false);
    setError('Failed to load preview');
  };

  const refresh = () => {
    setIsLoading(true);
    setError(null);
    if (iframeRef.current) {
      iframeRef.current.src = previewUrl;
    }
  };

  const openInNewTab = () => {
    window.open(previewUrl, '_blank');
  };

  return (
    <div className={`sprite-preview ${className} ${isExpanded ? 'expanded' : ''}`}>
      <div className="sprite-preview-header">
        <span className="sprite-preview-title">Preview (port 8080)</span>
        <div className="sprite-preview-actions">
          <button onClick={refresh} title="Refresh" className="sprite-preview-btn">
            ↻
          </button>
          <button onClick={openInNewTab} title="Open in new tab" className="sprite-preview-btn">
            ↗
          </button>
          <button
            onClick={() => setIsExpanded(!isExpanded)}
            title={isExpanded ? 'Collapse' : 'Expand'}
            className="sprite-preview-btn"
          >
            {isExpanded ? '⊟' : '⊞'}
          </button>
        </div>
      </div>

      <div className="sprite-preview-content">
        {isLoading && (
          <div className="sprite-preview-loading">
            Loading...
          </div>
        )}

        {error && (
          <div className="sprite-preview-error">
            {error}
            <button onClick={refresh} className="sprite-preview-retry">
              Retry
            </button>
          </div>
        )}

        <iframe
          ref={iframeRef}
          src={previewUrl}
          onLoad={handleLoad}
          onError={handleError}
          className="sprite-preview-iframe"
          sandbox="allow-scripts allow-same-origin allow-forms allow-popups"
          title="Sprite Preview"
        />
      </div>
    </div>
  );
}

// Styles to add to styles.css:
/*
.sprite-preview {
  display: flex;
  flex-direction: column;
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: hidden;
  background: var(--bg-card);
  min-height: 300px;
}

.sprite-preview.expanded {
  position: fixed;
  top: 20px;
  left: 20px;
  right: 20px;
  bottom: 20px;
  z-index: 1000;
  min-height: auto;
}

.sprite-preview-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 12px;
  background: var(--bg);
  border-bottom: 1px solid var(--border);
}

.sprite-preview-title {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.sprite-preview-actions {
  display: flex;
  gap: 4px;
}

.sprite-preview-btn {
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 4px;
  color: var(--text-muted);
  cursor: pointer;
  padding: 4px 8px;
  font-size: 14px;
}

.sprite-preview-btn:hover {
  background: var(--bg);
  color: var(--text);
}

.sprite-preview-content {
  flex: 1;
  position: relative;
  min-height: 0;
}

.sprite-preview-iframe {
  width: 100%;
  height: 100%;
  border: none;
  background: white;
}

.sprite-preview-loading,
.sprite-preview-error {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  background: var(--bg-card);
  color: var(--text-muted);
  gap: 12px;
}

.sprite-preview-retry {
  background: var(--accent);
  color: white;
  border: none;
  padding: 8px 16px;
  border-radius: 4px;
  cursor: pointer;
}
*/
