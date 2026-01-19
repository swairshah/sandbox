import { useState, useEffect, useRef, useCallback } from "react";
import { useAuth } from "./AuthContext";
import FileExplorer from "./FileExplorer";
import Terminal from "./Terminal";
import CodeViewer from "./CodeViewer";

type MessageStatus = "sending" | "queued" | "processing" | "completed" | "error" | "cancelled";

interface Message {
  id: string;
  type: "user" | "assistant" | "system" | "tool";
  content: string;
  tool?: ToolEvent;
  status?: MessageStatus;
  queuePosition?: number;
}

interface ToolEvent {
  type: "tool_use" | "tool_result";
  name?: string;
  input?: unknown;
  tool_use_id?: string;
  content?: unknown;
  is_error?: boolean;
}

interface WebSocketMessage {
  type: string;
  message_id?: string;
  content?: string;
  session_id?: string;
  tool_events?: ToolEvent[];
  queue_position?: number;
  queue_remaining?: number;
  error?: string;
  reason?: string;
  user_id?: string;
  status?: string;
  is_processing?: boolean;
  queue_size?: number;
  max_queue_size?: number;
  action?: string;
}

function getInitialTheme(): boolean {
  const saved = localStorage.getItem("monios-theme");
  if (saved !== null) {
    return saved === "dark";
  }
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

function generateId(): string {
  return Math.random().toString(36).substring(2, 9);
}

function getInitialGuestId(): string {
  return localStorage.getItem("monios-guest-user") || "guest";
}

export default function App() {
  const auth = useAuth();
  const [dark, setDark] = useState(getInitialTheme);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [guestId, setGuestId] = useState(getInitialGuestId);
  const [editingUser, setEditingUser] = useState(false);
  const [wsConnected, setWsConnected] = useState(false);
  const [queueStatus, setQueueStatus] = useState<{ size: number; processing: boolean }>({
    size: 0,
    processing: false,
  });
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [isDraggingOver, setIsDraggingOver] = useState(false);
  const [activePanel, setActivePanel] = useState<"chat" | "terminal" | "viewer" | "preview">("chat");
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [viewerReadOnly] = useState(true); // Config: set to false to enable editing
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const userInputRef = useRef<HTMLInputElement>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<number | null>(null);
  const toolUseNamesRef = useRef<Record<string, string>>({});

  // Get current user identifier (email for auth, guestId for guests)
  const userId = auth.isAuthenticated ? auth.user?.email || "user" : guestId;
  const userIdRef = useRef(userId); // Track latest userId for WebSocket callbacks

  // Keep userIdRef in sync
  useEffect(() => {
    userIdRef.current = userId;
  }, [userId]);

  // Load chat history from server
  const loadChatHistory = useCallback(async (uid: string) => {
    try {
      const response = await fetch(`/chat/history?user_id=${encodeURIComponent(uid)}`);
      if (!response.ok) return;
      
      const data = await response.json();
      if (data.messages && data.messages.length > 0) {
        const loadedMessages: Message[] = [];
        for (const msg of data.messages) {
          loadedMessages.push({
            id: msg.id || generateId(),
            type: msg.role === "user" ? "user" : "assistant",
            content: msg.content,
            status: "completed" as MessageStatus,
          });
          // Add tool events if any
          if (msg.tool_events) {
            for (const event of msg.tool_events) {
              loadedMessages.push({
                id: generateId(),
                type: "tool",
                content: "",
                tool: event,
              });
            }
          }
        }
        setMessages(loadedMessages);
      }
    } catch (e) {
      console.error("Failed to load chat history:", e);
    }
  }, []);

  // Update message status helper
  const updateMessageStatus = useCallback(
    (messageId: string, status: MessageStatus, queuePosition?: number) => {
      setMessages((prev) =>
        prev.map((msg) =>
          msg.id === messageId ? { ...msg, status, queuePosition } : msg
        )
      );
    },
    []
  );

  // Add assistant response for a message
  const addAssistantResponse = useCallback(
    (messageId: string, content: string, toolEvents?: ToolEvent[]) => {
      setMessages((prev) => {
        // Mark the user message as completed
        const updated = prev.map((msg) =>
          msg.id === messageId ? { ...msg, status: "completed" as MessageStatus } : msg
        );

        // Add tool events if any
        const newMessages: Message[] = [];
        if (toolEvents) {
          for (const event of toolEvents) {
            newMessages.push({
              id: generateId(),
              type: "tool",
              content: "",
              tool: event,
            });
          }
        }

        // Add assistant response
        if (content) {
          newMessages.push({
            id: generateId(),
            type: "assistant",
            content,
          });
        }

        return [...updated, ...newMessages];
      });
    },
    []
  );

  // Handle WebSocket messages
  const handleWebSocketMessage = useCallback(
    (event: MessageEvent) => {
      try {
        const data: WebSocketMessage = JSON.parse(event.data);
        console.log("WS received:", data);

        switch (data.type) {
          case "connected":
            setWsConnected(true);
            setError(null);
            break;

          case "queued":
            if (data.message_id) {
              const status = data.status === "skipped" ? "cancelled" : "queued";
              updateMessageStatus(
                data.message_id,
                status as MessageStatus,
                data.queue_position
              );
              if (data.status === "queue_full") {
                setError(`Queue is full (max ${data.queue_size} messages)`);
              }
            }
            break;

          case "processing_started":
            if (data.message_id) {
              updateMessageStatus(data.message_id, "processing");
              setQueueStatus((prev) => ({
                ...prev,
                processing: true,
                size: data.queue_remaining ?? prev.size,
              }));
            }
            break;

          case "tool_use":
            // Stream tool use as it happens
            if (data.message_id) {
              if (data.tool_use_id && data.name) {
                toolUseNamesRef.current[data.tool_use_id] = data.name;
              }
              setMessages((prev) => [
                ...prev,
                {
                  id: generateId(),
                  type: "tool",
                  content: "",
                  tool: {
                    type: "tool_use",
                    name: data.name,
                    input: data.input,
                    tool_use_id: data.tool_use_id,
                  },
                },
              ]);
            }
            break;

          case "tool_result":
            // Stream tool result as it happens
            if (data.message_id) {
              const toolName = data.tool_use_id
                ? toolUseNamesRef.current[data.tool_use_id]
                : undefined;
              const shouldRefreshFiles =
                !toolName ||
                toolName === "Write" ||
                toolName === "Edit" ||
                toolName === "Bash" ||
                toolName.endsWith("__Write") ||
                toolName.endsWith("__Edit") ||
                toolName.endsWith("__Bash");
              if (shouldRefreshFiles) {
                window.dispatchEvent(new CustomEvent("monios:file-refresh"));
              }
              setMessages((prev) => [
                ...prev,
                {
                  id: generateId(),
                  type: "tool",
                  content: "",
                  tool: {
                    type: "tool_result",
                    tool_use_id: data.tool_use_id,
                    content: data.content,
                    is_error: data.is_error,
                  },
                },
              ]);
            }
            break;

          case "response":
            if (data.message_id) {
              // Don't add tool_events here since they were already streamed
              addAssistantResponse(
                data.message_id,
                data.content || "",
                undefined // Tool events already added via streaming
              );
              setQueueStatus((prev) => ({ ...prev, processing: false }));
            }
            break;

          case "error":
            if (data.message_id) {
              updateMessageStatus(data.message_id, "error");
            }
            setError(data.error || "Unknown error");
            setQueueStatus((prev) => ({ ...prev, processing: false }));
            break;

          case "cancelled":
            if (data.message_id) {
              updateMessageStatus(data.message_id, "cancelled");
              // Add system message about cancellation
              setMessages((prev) => [
                ...prev,
                {
                  id: generateId(),
                  type: "system",
                  content: `Message cancelled: ${data.reason || "cancelled"}`,
                },
              ]);
            }
            setQueueStatus((prev) => ({ ...prev, processing: false }));
            break;

          case "status":
            setQueueStatus({
              size: data.queue_size ?? 0,
              processing: data.is_processing ?? false,
            });
            break;
        }
      } catch (e) {
        console.error("Failed to parse WebSocket message:", e);
      }
    },
    [updateMessageStatus, addAssistantResponse]
  );

  // Connect to WebSocket
  const connectWebSocket = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      return;
    }

    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${protocol}//${window.location.host}/ws/chat`);

    ws.onopen = () => {
      console.log("WebSocket connected");
      // Send connect message with user_id - use ref for latest value
      ws.send(JSON.stringify({ type: "connect", user_id: userIdRef.current }));
    };

    ws.onmessage = handleWebSocketMessage;

    ws.onerror = (e) => {
      console.error("WebSocket error:", e);
      setError("Connection error");
    };

    ws.onclose = () => {
      console.log("WebSocket closed");
      setWsConnected(false);
      // Attempt to reconnect after 3 seconds
      reconnectTimeoutRef.current = window.setTimeout(() => {
        connectWebSocket();
      }, 3000);
    };

    wsRef.current = ws;
  }, [handleWebSocketMessage]); // No userId dep - uses userIdRef for latest value

  // Initialize WebSocket connection
  useEffect(() => {
    connectWebSocket();

    return () => {
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
      }
      if (wsRef.current) {
        wsRef.current.close();
      }
    };
  }, [connectWebSocket]);

  // Reconnect WebSocket and load messages when userId changes
  useEffect(() => {
    // Clear any pending reconnect timeout
    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
      reconnectTimeoutRef.current = null;
    }
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
      setWsConnected(false);
      setMessages([]); // Clear current messages first
      loadChatHistory(userId); // Load history for new user
      connectWebSocket();
    }
  }, [userId, connectWebSocket, loadChatHistory]);

  // Load chat history on initial mount
  useEffect(() => {
    loadChatHistory(userId);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
  }, [dark]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const toggleTheme = () => {
    const newDark = !dark;
    setDark(newDark);
    localStorage.setItem("monios-theme", newDark ? "dark" : "light");
  };

  const clearChat = async () => {
    setMessages([]);
    setError(null);
    try {
      if (auth.isAuthenticated) {
        // Use authenticated endpoint
        await fetch("/api/chat/clear", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            ...auth.getAuthHeaders(),
          },
        });
      } else {
        // Use guest endpoint
        await fetch("/chat/clear", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ user_id: guestId }),
        });
      }
    } catch {
      // Ignore clear errors
    }
  };

  const saveGuestId = (newId: string) => {
    const trimmed = newId.trim() || "guest";
    setGuestId(trimmed);
    localStorage.setItem("monios-guest-user", trimmed);
    setEditingUser(false);
  };

  useEffect(() => {
    if (editingUser && userInputRef.current) {
      userInputRef.current.focus();
      userInputRef.current.select();
    }
  }, [editingUser]);

  const sendMessage = () => {
    const trimmed = input.trim();
    if (!trimmed) return;

    // Check if WebSocket is connected
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      setError("Not connected. Reconnecting...");
      connectWebSocket();
      return;
    }

    setInput("");
    setError(null);

    const messageId = generateId();
    const userMsg: Message = {
      id: messageId,
      type: "user",
      content: trimmed,
      status: "sending",
    };
    setMessages((prev) => [...prev, userMsg]);

    // Send message via WebSocket
    wsRef.current.send(
      JSON.stringify({
        type: "message",
        content: trimmed,
        message_id: messageId,
      })
    );
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  };

  const adjustTextareaHeight = () => {
    const textarea = textareaRef.current;
    if (textarea) {
      textarea.style.height = "auto";
      textarea.style.height = Math.min(textarea.scrollHeight, 200) + "px";
    }
  };

  // Drag and drop handlers for file explorer
  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDraggingOver(true);
  };

  const handleDragLeave = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDraggingOver(false);
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDraggingOver(false);

    const path = e.dataTransfer.getData("text/plain");
    if (path) {
      // Insert path at cursor position or append
      const textarea = textareaRef.current;
      if (textarea) {
        const start = textarea.selectionStart;
        const end = textarea.selectionEnd;
        const currentValue = input;
        const newValue = currentValue.substring(0, start) + path + currentValue.substring(end);
        setInput(newValue);
        // Set cursor after inserted path
        setTimeout(() => {
          textarea.focus();
          textarea.setSelectionRange(start + path.length, start + path.length);
        }, 0);
      } else {
        setInput((prev) => prev + path);
      }
    }
  };

  const toggleSidebar = () => setSidebarOpen((prev) => !prev);

  // Handle file selection from FileExplorer
  const handleFileSelect = (path: string, isDirectory: boolean) => {
    if (!isDirectory) {
      setSelectedFile(path);
      setActivePanel("viewer");
    }
  };

  // Close the viewer
  const handleCloseViewer = () => {
    setSelectedFile(null);
    setActivePanel("chat");
  };

  // Fetch preview URL
  const fetchPreviewUrl = useCallback(async () => {
    setPreviewLoading(true);
    try {
      const response = await fetch(`/preview?user_id=${encodeURIComponent(userId)}`);
      const data = await response.json();
      if (data.preview_url) {
        setPreviewUrl(data.preview_url);
      }
    } catch (e) {
      console.error("Failed to fetch preview URL:", e);
    } finally {
      setPreviewLoading(false);
    }
  }, [userId]);

  // Refresh preview URL when switching to preview tab
  const handlePreviewTab = useCallback(() => {
    setActivePanel("preview");
    fetchPreviewUrl();
  }, [fetchPreviewUrl]);

  return (
    <div className="app">
      <header>
        <button
          className="sidebar-toggle"
          onClick={toggleSidebar}
          title={sidebarOpen ? "Hide sidebar" : "Show sidebar"}
        >
          {sidebarOpen ? "\u2630" : "\u2630"}
        </button>
        <span className="logo">monios</span>
        <div className="header-actions">
          {auth.isAuthenticated ? (
            // Authenticated user display
            <>
              {auth.user?.picture && (
                <img
                  src={auth.user.picture}
                  alt=""
                  className="user-avatar"
                />
              )}
              <span className="user-email">{auth.user?.email}</span>
              <button className="signout-btn" onClick={auth.signOut}>
                sign out
              </button>
            </>
          ) : (
            // Guest mode with login option
            <>
              {editingUser ? (
                <input
                  ref={userInputRef}
                  className="user-input"
                  defaultValue={guestId}
                  onBlur={(e) => saveGuestId(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") saveGuestId(e.currentTarget.value);
                    if (e.key === "Escape") setEditingUser(false);
                  }}
                />
              ) : (
                <button
                  className="user-btn"
                  onClick={() => setEditingUser(true)}
                  title="Click to change guest name"
                >
                  @{guestId}
                </button>
              )}
              <button
                className="google-signin-btn"
                onClick={auth.signInWithGoogle}
                disabled={auth.isLoading}
              >
                {auth.isLoading ? "..." : "sign in"}
              </button>
            </>
          )}
          <button className="clear-btn" onClick={clearChat}>
            clear
          </button>
          <button className="theme-toggle" onClick={toggleTheme}>
            {dark ? "\u2600" : "\u263E"}
          </button>
        </div>
      </header>

      <div className="main-layout">
        {sidebarOpen && (
          <aside className="sidebar">
            <FileExplorer onFileSelect={handleFileSelect} userId={userId} />
          </aside>
        )}

        <div className="chat-container">
          <div className="panel-tabs">
            <button
              className={`panel-tab ${activePanel === "chat" ? "active" : ""}`}
              onClick={() => setActivePanel("chat")}
            >
              Chat
            </button>
            <button
              className={`panel-tab ${activePanel === "terminal" ? "active" : ""}`}
              onClick={() => setActivePanel("terminal")}
            >
              Terminal
            </button>
            <button
              className={`panel-tab ${activePanel === "viewer" ? "active" : ""}`}
              onClick={() => setActivePanel("viewer")}
            >
              Viewer
              {selectedFile && <span className="tab-indicator">*</span>}
            </button>
            <button
              className={`panel-tab ${activePanel === "preview" ? "active" : ""}`}
              onClick={handlePreviewTab}
            >
              Preview
              {previewUrl && <span className="tab-indicator">*</span>}
            </button>
          </div>

          <div className={`panel-content ${activePanel === "chat" ? "active" : ""}`}>
            <div className="messages">
              {messages.length === 0 && (
                <div className="message system">
                  {auth.isAuthenticated
                    ? `signed in as ${auth.user?.email}. send a message to start chatting`
                    : "send a message to start chatting (or sign in with Google)"}
                </div>
              )}

            {messages.map((msg) => (
              <div
                key={msg.id}
                className={`message ${msg.type === "tool" ? "assistant" : msg.type} ${msg.status ? `status-${msg.status}` : ""}`}
              >
                {msg.type === "tool" && msg.tool ? (
                  msg.tool.type === "tool_use" ? (
                    <div className="tool-use">
                      <div className="tool-name">tool: {msg.tool.name}</div>
                      <div className="tool-input">
                        {JSON.stringify(msg.tool.input ?? {}, null, 2)}
                      </div>
                    </div>
                  ) : (
                    <div className="tool-result">
                      {JSON.stringify(msg.tool.content ?? "", null, 2)}
                    </div>
                  )
                ) : (
                  <>
                    <div className="message-content">{msg.content}</div>
                    {msg.type === "user" && msg.status && msg.status !== "completed" && (
                      <div className="message-status">
                        {msg.status === "sending" && (
                          <span className="status-indicator sending">sending...</span>
                        )}
                        {msg.status === "queued" && (
                          <span className="status-indicator queued">
                            queued{msg.queuePosition ? ` (#${msg.queuePosition})` : ""}
                          </span>
                        )}
                        {msg.status === "processing" && (
                          <span className="status-indicator processing">
                            <span className="status-dot loading"></span>
                            processing...
                          </span>
                        )}
                        {msg.status === "error" && (
                          <span className="status-indicator error">error</span>
                        )}
                        {msg.status === "cancelled" && (
                          <span className="status-indicator cancelled">cancelled</span>
                        )}
                      </div>
                    )}
                  </>
                )}
              </div>
            ))}

            {error && (
              <div className="error">{error}</div>
            )}

            <div ref={messagesEndRef} />
          </div>

          <div
            className={`input-area ${isDraggingOver ? "drag-over" : ""}`}
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop}
          >
            {isDraggingOver && (
              <div className="drop-indicator">Drop file path here</div>
            )}
            {(queueStatus.size > 0 || queueStatus.processing) && (
              <div className="queue-status">
                {queueStatus.processing && <span className="status-dot processing"></span>}
                {queueStatus.size > 0 && <span>queue: {queueStatus.size}</span>}
              </div>
            )}
            <textarea
              ref={textareaRef}
              value={input}
              onChange={(e) => {
                setInput(e.target.value);
                adjustTextareaHeight();
              }}
              onKeyDown={handleKeyDown}
              placeholder={wsConnected ? "type a message... (drag files here)" : "connecting..."}
              rows={1}
            />
            <button
              className="send-btn"
              onClick={sendMessage}
              disabled={!input.trim() || !wsConnected}
            >
              send
            </button>
            </div>
          </div>

          <div className={`panel-content ${activePanel === "terminal" ? "active" : ""}`}>
            <Terminal userId={userId} />
          </div>

          <div className={`panel-content ${activePanel === "viewer" ? "active" : ""}`}>
            <CodeViewer
              filePath={selectedFile}
              readOnly={viewerReadOnly}
              onClose={handleCloseViewer}
              userId={userId}
            />
          </div>

          <div className={`panel-content ${activePanel === "preview" ? "active" : ""}`}>
            <div className="preview-panel">
              {previewLoading ? (
                <div className="preview-loading">Loading preview...</div>
              ) : previewUrl ? (
                <>
                  <div className="preview-toolbar">
                    <span className="preview-url">{previewUrl}</span>
                    <button
                      className="preview-refresh"
                      onClick={fetchPreviewUrl}
                      title="Refresh preview URL"
                    >
                      Refresh
                    </button>
                    <a
                      href={previewUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="preview-open"
                    >
                      Open in new tab
                    </a>
                  </div>
                  <iframe
                    src={previewUrl}
                    className="preview-iframe"
                    title="App Preview"
                    sandbox="allow-scripts allow-same-origin allow-forms allow-popups"
                  />
                </>
              ) : (
                <div className="preview-empty">
                  <p>No preview available.</p>
                  <p>Start a dev server on port 3000 in the terminal to see a preview here.</p>
                  <button onClick={fetchPreviewUrl} className="preview-check-btn">
                    Check for preview
                  </button>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
