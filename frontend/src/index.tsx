import { createRoot } from "react-dom/client";
import "./styles.css";
import App from "./App";
import { AuthProvider } from "./AuthContext";

const root = createRoot(document.getElementById("root")!);
root.render(
  <AuthProvider>
    <App />
  </AuthProvider>
);
