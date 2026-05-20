import ReactDOM from 'react-dom/client'
import App from './App.tsx'
import { ErrorBoundary } from './components/ErrorBoundary.tsx'
import { ToastContainer } from './components/ToastContainer.tsx'
import './index.css'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <ErrorBoundary>
    <App />
    <ToastContainer />
  </ErrorBoundary>,
)
