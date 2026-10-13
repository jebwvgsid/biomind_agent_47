import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "highlight.js/styles/github.css";
import "./styles.css";
import { WorkbenchErrorBoundary } from "./components/WorkbenchErrorBoundary";
import { installRendererErrorHandlers } from "./lib/rendererSafety";

installRendererErrorHandlers();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <WorkbenchErrorBoundary><App /></WorkbenchErrorBoundary>
  </React.StrictMode>,
);
