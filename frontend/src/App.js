import { useState } from 'react';
import { BrowserRouter as Router, Routes, Route, Navigate } from 'react-router-dom';
import { Layout } from 'antd';
import styled from '@emotion/styled';
import Login from './pages/Login';
import Home from './pages/Home';
import ImageList from './pages/ImageList';
import FileList from './pages/FileList';
import SharedFiles from './pages/SharedFiles';
import TopDownloads from './pages/TopDownloads';
import NavBar from './components/NavBar';
import ChatPanel from './components/ChatPanel';
import { AuthProvider, useAuth } from './contexts/AuthContext';

const { Content } = Layout;

const StyledLayout = styled(Layout)`
  background: linear-gradient(135deg, rgba(200, 255, 200, 0.4), rgba(150, 255, 150, 0.2));
  min-height: 100vh;
`;

const GlassContent = styled(Content)`
  background: rgba(255, 255, 255, 0.25);
  backdrop-filter: blur(10px);
  border-radius: 16px;
  border: 1px solid rgba(255, 255, 255, 0.3);
  margin: 24px;
  padding: 24px;
  box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
`;

const PrivateRoute = ({ children }) => {
  const { user } = useAuth();
  return user ? children : <Navigate to="/login" replace />;
};

function AppRoutes() {
  const { user } = useAuth();
  const [chatOpen, setChatOpen] = useState(false);

  return (
    <StyledLayout>
      <NavBar
        onChatToggle={() => setChatOpen((v) => !v)}
        chatOpen={chatOpen}
      />
      <GlassContent>
        <Routes>
          <Route path="/login" element={user ? <Navigate to="/" replace /> : <Login />} />
          <Route path="/" element={<PrivateRoute><Home /></PrivateRoute>} />
          <Route path="/images" element={<PrivateRoute><ImageList /></PrivateRoute>} />
          <Route path="/files" element={<PrivateRoute><FileList /></PrivateRoute>} />
          <Route path="/shared" element={<PrivateRoute><SharedFiles /></PrivateRoute>} />
          <Route path="/top-downloads" element={<PrivateRoute><TopDownloads /></PrivateRoute>} />
        </Routes>
      </GlassContent>
      {user && <ChatPanel open={chatOpen} onClose={() => setChatOpen(false)} />}
    </StyledLayout>
  );
}

function App() {
  return (
    <AuthProvider>
      <Router>
        <AppRoutes />
      </Router>
    </AuthProvider>
  );
}

export default App;
