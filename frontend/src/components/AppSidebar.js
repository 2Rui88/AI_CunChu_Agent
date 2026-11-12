import React from 'react';
import { Layout, Menu, Tooltip } from 'antd';
import {
  HomeOutlined,
  PictureOutlined,
  FolderOpenOutlined,
  ShareAltOutlined,
  TrophyOutlined,
  LogoutOutlined,
  CloudUploadOutlined,
  ApartmentOutlined,
  ExperimentOutlined,
  TeamOutlined,
} from '@ant-design/icons';
import styled from '@emotion/styled';
import { useNavigate, useLocation } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import UserAvatar from './UserAvatar';

const { Sider } = Layout;

const SideWrapper = styled.div`
  display: flex;
  flex-direction: column;
  height: 100%;
`;

const LogoBox = styled.div`
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 20px 16px 14px;
`;

const LogoIcon = styled.div`
  width: 36px;
  height: 36px;
  border-radius: 10px;
  background: linear-gradient(135deg, #2563eb, #7c3aed);
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-size: 18px;
  flex: none;
`;

const LogoName = styled.div`
  font-size: 16px;
  font-weight: 700;
  color: #1f2329;
  line-height: 1.2;
  span {
    display: block;
    font-size: 11px;
    font-weight: 400;
    color: #8c95a3;
  }
`;

const MenuWrap = styled.div`
  flex: 1;
  padding: 6px 10px;
  overflow-y: auto;
`;

const UserBox = styled.div`
  margin: 10px;
  padding: 10px;
  border-radius: 12px;
  background: #f5f7fa;
  display: flex;
  align-items: center;
  gap: 10px;
`;

const UserInfo = styled.div`
  flex: 1;
  min-width: 0;
  .name {
    font-size: 13px;
    font-weight: 600;
    color: #1f2329;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .status {
    font-size: 11px;
    color: #22c55e;
  }
`;

// roles: 可见角色限定（不填=全员可见）——部门文件仅主管可见
const MENU_ITEMS = [
  { key: '/', icon: <HomeOutlined />, label: '首页' },
  { key: '/files', icon: <FolderOpenOutlined />, label: '文件管理' },
  { key: '/images', icon: <PictureOutlined />, label: '图片库' },
  { key: '/shared', icon: <ShareAltOutlined />, label: '共享广场' },
  { key: '/top-downloads', icon: <TrophyOutlined />, label: '下载排行' },
  { key: '/dept', icon: <TeamOutlined />, label: '部门文件', roles: ['supervisor'] },
  { key: '/vector-docs', icon: <ApartmentOutlined />, label: '向量化文档' },
  { key: '/eval', icon: <ExperimentOutlined />, label: 'RAG 评估' },
];

const ROLE_TEXT = { supervisor: '主管', member: '成员' };

export default function AppSidebar() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const items = MENU_ITEMS.filter((it) => !it.roles || it.roles.includes(user?.role));

  return (
    <Sider
      width={224}
      theme="light"
      breakpoint="lg"
      collapsedWidth={0}
      trigger={null}
      style={{ borderRight: '1px solid #eef0f4' }}
    >
      <SideWrapper>
        <LogoBox>
          <LogoIcon>
            <CloudUploadOutlined />
          </LogoIcon>
          <LogoName>
            AI 云存储
            <span>YunCunChu Drive</span>
          </LogoName>
        </LogoBox>

        <MenuWrap>
          <Menu
            mode="inline"
            selectedKeys={[location.pathname]}
            items={items}
            onClick={({ key }) => navigate(key)}
            style={{ borderInlineEnd: 'none', background: 'transparent' }}
          />
        </MenuWrap>

        <UserBox>
          <UserAvatar name={user?.username} size={34} />
          <UserInfo>
            <div className="name">{user?.username}</div>
            <div className="status">
              ● {[user?.department, ROLE_TEXT[user?.role] || '成员'].filter(Boolean).join(' · ')}
            </div>
          </UserInfo>
          <Tooltip title="退出登录">
            <LogoutOutlined
              onClick={logout}
              style={{ color: '#8c95a3', cursor: 'pointer', fontSize: 15 }}
            />
          </Tooltip>
        </UserBox>
      </SideWrapper>
    </Sider>
  );
}
