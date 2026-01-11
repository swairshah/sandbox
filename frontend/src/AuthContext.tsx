import { createContext, useContext, useState, useEffect, useCallback, ReactNode } from "react";

// Google Client ID (web application)
const GOOGLE_CLIENT_ID = "558486289958-j768tfatvm4mqkpji50vc3tgo85kf01q.apps.googleusercontent.com";

interface User {
  id: string;
  email: string;
  name?: string;
  picture?: string;
}

interface AuthState {
  user: User | null;
  accessToken: string | null;
  refreshToken: string | null;
  isAuthenticated: boolean;
  isLoading: boolean;
}

interface AuthContextType extends AuthState {
  signInWithGoogle: () => void;
  signOut: () => void;
  getAuthHeaders: () => Record<string, string>;
}

const AuthContext = createContext<AuthContextType | null>(null);

// Token storage keys
const ACCESS_TOKEN_KEY = "monios-access-token";
const REFRESH_TOKEN_KEY = "monios-refresh-token";
const USER_KEY = "monios-auth-user";

// Declare google global for TypeScript
declare global {
  interface Window {
    google?: {
      accounts: {
        id: {
          initialize: (config: {
            client_id: string;
            callback: (response: { credential: string }) => void;
            auto_select?: boolean;
          }) => void;
          prompt: () => void;
          renderButton: (
            element: HTMLElement,
            config: {
              theme?: string;
              size?: string;
              text?: string;
              shape?: string;
              width?: number;
            }
          ) => void;
          revoke: (email: string, callback: () => void) => void;
        };
      };
    };
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>(() => {
    // Try to restore auth state from localStorage
    const savedUser = localStorage.getItem(USER_KEY);
    const accessToken = localStorage.getItem(ACCESS_TOKEN_KEY);
    const refreshToken = localStorage.getItem(REFRESH_TOKEN_KEY);

    if (savedUser && accessToken) {
      try {
        return {
          user: JSON.parse(savedUser),
          accessToken,
          refreshToken,
          isAuthenticated: true,
          isLoading: false,
        };
      } catch {
        // Invalid saved state, clear it
        localStorage.removeItem(USER_KEY);
        localStorage.removeItem(ACCESS_TOKEN_KEY);
        localStorage.removeItem(REFRESH_TOKEN_KEY);
      }
    }

    return {
      user: null,
      accessToken: null,
      refreshToken: null,
      isAuthenticated: false,
      isLoading: false,
    };
  });

  const handleGoogleCallback = useCallback(async (response: { credential: string }) => {
    setState(prev => ({ ...prev, isLoading: true }));

    try {
      // Send the Google ID token to our backend
      const res = await fetch("/auth/google", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id_token: response.credential }),
      });

      if (!res.ok) {
        throw new Error("Authentication failed");
      }

      const data = await res.json();
      const { user, tokens } = data;

      // Save to localStorage
      localStorage.setItem(USER_KEY, JSON.stringify(user));
      localStorage.setItem(ACCESS_TOKEN_KEY, tokens.access_token);
      localStorage.setItem(REFRESH_TOKEN_KEY, tokens.refresh_token);

      setState({
        user,
        accessToken: tokens.access_token,
        refreshToken: tokens.refresh_token,
        isAuthenticated: true,
        isLoading: false,
      });
    } catch (error) {
      console.error("Auth error:", error);
      setState(prev => ({ ...prev, isLoading: false }));
    }
  }, []);

  // Initialize Google Sign-In when the component mounts
  useEffect(() => {
    const initGoogle = () => {
      if (window.google) {
        window.google.accounts.id.initialize({
          client_id: GOOGLE_CLIENT_ID,
          callback: handleGoogleCallback,
          auto_select: false,
        });
      }
    };

    // Check if google is already loaded
    if (window.google) {
      initGoogle();
    } else {
      // Wait for the script to load
      const checkGoogle = setInterval(() => {
        if (window.google) {
          initGoogle();
          clearInterval(checkGoogle);
        }
      }, 100);

      // Clean up after 10 seconds
      setTimeout(() => clearInterval(checkGoogle), 10000);
    }
  }, [handleGoogleCallback]);

  const signInWithGoogle = useCallback(() => {
    if (window.google) {
      window.google.accounts.id.prompt();
    }
  }, []);

  const signOut = useCallback(() => {
    // Clear local storage
    localStorage.removeItem(USER_KEY);
    localStorage.removeItem(ACCESS_TOKEN_KEY);
    localStorage.removeItem(REFRESH_TOKEN_KEY);

    // Revoke Google session if we have a user email
    if (state.user?.email && window.google) {
      window.google.accounts.id.revoke(state.user.email, () => {
        console.log("Google session revoked");
      });
    }

    setState({
      user: null,
      accessToken: null,
      refreshToken: null,
      isAuthenticated: false,
      isLoading: false,
    });
  }, [state.user?.email]);

  const getAuthHeaders = useCallback((): Record<string, string> => {
    if (state.accessToken) {
      return { Authorization: `Bearer ${state.accessToken}` };
    }
    return {};
  }, [state.accessToken]);

  return (
    <AuthContext.Provider
      value={{
        ...state,
        signInWithGoogle,
        signOut,
        getAuthHeaders,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return context;
}
