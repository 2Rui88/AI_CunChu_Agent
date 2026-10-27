import { BrowserRouter as Router, Routes, Route, Navigate, useLocation } from 'react-router-dom';
import { ConfigProvider, Layout } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import AppSidebar from './components/AppSidebar';
import PageHeader from './components/PageHeader';
import Login from './pages/Login';
import Home from './pages/Home';
import ImageList from './pages/ImageList';
import FileList from './pages/FileList';
import SharedFiles from './pages/SharedFiles';
import TopDownloads from './pages/TopDownloads';
import { AuthProvider, useAuth } from './contexts/AuthContext';

const { Content } = Layout;

const themeConfig = {
  token: {
    colorPrimary: '#2563eb',
    colorLink: '#2563eb',
    borderRadius: 10,
    colorBgLayout: '#f5f7fa',
    fontSize: 14,
  },
  components: {
    Menu: {
      itemBorderRadius: 10,
      itemMarginInline: 4,
      itemHeight: 44,
    },
    Card: {
      paddingLG: 20,
    },
  },
};

const PAGE_META = {
  '/': { title: '首页', desc: '与 AI 助手对话：描述内容即可搜文件，还能统计、分享、删除' },
  '/files': { title: '文件管理', desc: '上传、下载、分享普通文件，大文件自动分片续传' },
  '/images': { title: '图片库', desc: '图片上传与图床分享，AI 自动生成图片描述' },
  '/shared': { title: '共享广场', desc: '其他用户分享的文件，可转存到自己的空间' },
  '/top-downloads': { title: '下载排行', desc: '全站共享文件下载量 TOP 10' },
};

const PrivateRoute = ({ children }) => {
  const { user } = useAuth();
  return user ? children : <Navigate to="/login" replace />;
};

function MainLayout() {
  const { user } = useAuth();
  const location = useLocation();
  const meta = PAGE_META[location.pathname] || PAGE_META['/'];

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <AppSidebar />
      <Layout>
        <PageHeader title={meta.title} desc={meta.desc} />
        <Content style={{ padding: '24px 24px 40px' }}>
          <div style={{ maxWidth: 1160, margin: '0 auto' }}>
            <Routes>
              <Route path="/" element={<PrivateRoute><Home /></PrivateRoute>} />
              <Route path="/files" element={<PrivateRoute><FileList /></PrivateRoute>} />
              <Route path="/images" element={<PrivateRoute><ImageList /></PrivateRoute>} />
              <Route path="/shared" element={<PrivateRoute><SharedFiles /></PrivateRoute>} />
              <Route path="/top-downloads" element={<PrivateRoute><TopDownloads /></PrivateRoute>} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </div>
        </Content>
      </Layout>
    </Layout>
  );
}

function AppRoutes() {
  const { user } = useAuth();
  const location = useLocation();

  if (location.pathname === '/login') {
    return user ? <Navigate to="/" replace /> : <Login />;
  }
  return <MainLayout />;
}

function App() {
  return (
    <ConfigProvider locale={zhCN} theme={themeConfig}>
      <AuthProvider>
        <Router>
          <AppRoutes />
        </Router>
      </AuthProvider>
    </ConfigProvider>
  );
}

export default App;
