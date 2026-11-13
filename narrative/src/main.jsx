import React from 'react';
import { createRoot } from 'react-dom/client';
import '@xyflow/react/dist/style.css';
import './style.css';
import App from './App.jsx';

class ErrorBoundary extends React.Component {
  state = { error: null };
  static getDerivedStateFromError(error) { return { error }; }
  render() {
    if (this.state.error) return <main className="fatal-error"><h1>编辑器遇到了问题</h1><p>请刷新页面重新加载已保存的项目。如仍然发生，请联系管理员。</p><pre>{this.state.error.message}</pre><button onClick={() => location.reload()}>重新加载</button></main>;
    return this.props.children;
  }
}

createRoot(document.getElementById('root')).render(<ErrorBoundary><App /></ErrorBoundary>);
