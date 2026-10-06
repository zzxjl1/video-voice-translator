
import React from 'react';
import ReactDOM from 'react-dom/client';
// Bundled and injected by Vite, so the app is styled on first paint instead of
// waiting for a CDN script to arrive and repaint it.
import './index.css';
import App from './App';

const rootElement = document.getElementById('root');
if (!rootElement) {
  throw new Error("Could not find root element to mount to");
}

const root = ReactDOM.createRoot(rootElement);
root.render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
