import React, { useState } from 'react';
import { Card, Form, Input, Button, Tabs, message } from 'antd';
import {
  UserOutlined, LockOutlined, SmileOutlined,
  CloudUploadOutlined, SearchOutlined, RobotOutlined,
} from '@ant-design/icons';
import styled from '@emotion/styled';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import { loginUser, registerUser } from '../services/auth';

const PageWrap = styled.div`
  display: flex;
  min-height: 100vh;
  background: #f5f7fa;
`;

const BrandPanel = styled.div`
  flex: 1;
  background: linear-gradient(160deg, #2563eb 0%, #4f46e5 55%, #7c3aed 100%);
  color: #fff;
  display: flex;
  flex-direction: column;
  justify-content: center;
  padding: 72px;
  @media (max-width: 880px) {
    display: none;
  }
`;

const BrandLogo = styled.div`
  display: flex;
  align-items: center;
  gap: 14px;
  margin-bottom: 48px;
`;

const BrandIcon = styled.div`
  width: 52px;
  height: 52px;
  border-radius: 14px;
  background: rgba(255, 255, 255, 0.18);
  backdrop-filter: blur(4px);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 26px;
`;

const BrandTitle = styled.h1`
  margin: 0;
  font-size: 30px;
  font-weight: 700;
  letter-spacing: 1px;
`;

const BrandSlogan = styled.p`
  font-size: 16px;
  opacity: 0.85;
  margin: 0 0 40px;
  line-height: 1.7;
`;

const FeatureItem = styled.div`
  display: flex;
  align-items: center;
  gap: 14px;
  margin-bottom: 22px;
`;

const FeatureIcon = styled.div`
  width: 42px;
  height: 42px;
  border-radius: 12px;
  background: rgba(255, 255, 255, 0.15);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 19px;
  flex: none;
`;

const FeatureText = styled.div`
  .t {
    font-size: 15px;
    font-weight: 600;
  }
  .d {
    font-size: 13px;
    opacity: 0.75;
    margin-top: 2px;
  }
`;

const FormSide = styled.div`
  width: 460px;
  max-width: 100vw;
  display: flex;
  justify-content: center;
  align-items: center;
  padding: 24px;
  background: #fff;
`;

const FormCard = styled(Card)`
  width: 380px;
  max-width: 100%;
  border: none;
  box-shadow: none;
`;

const FormTitle = styled.h2`
  text-align: center;
  color: #1f2329;
  margin: 0 0 4px;
`;

const FormSub = styled.div`
  text-align: center;
  color: #8c95a3;
  font-size: 13px;
  margin-bottom: 24px;
`;

function LoginForm() {
  const [loading, setLoading] = useState(false);
  const { login } = useAuth();
  const navigate = useNavigate();

  const onFinish = async ({ username, pwd }) => {
    setLoading(true);
    try {
      const token = await loginUser(username, pwd);
      login(username, token);
      message.success('登录成功');
      navigate('/', { replace: true });
    } catch (err) {
      message.error(err.message || '登录失败');
    } finally {
      setLoading(false);
    }
  };

  return (
    <Form onFinish={onFinish} autoComplete="off" size="large">
      <Form.Item name="username" rules={[{ required: true, message: '请输入用户名' }]}>
        <Input prefix={<UserOutlined style={{ color: '#b3bac4' }} />} placeholder="用户名" maxLength={32} />
      </Form.Item>
      <Form.Item name="pwd" rules={[{ required: true, message: '请输入密码' }]}>
        <Input.Password prefix={<LockOutlined style={{ color: '#b3bac4' }} />} placeholder="密码" maxLength={64} />
      </Form.Item>
      <Button type="primary" htmlType="submit" block loading={loading} style={{ marginTop: 4 }}>
        登 录
      </Button>
    </Form>
  );
}

function RegisterForm({ onRegistered }) {
  const [loading, setLoading] = useState(false);

  const onFinish = async (values) => {
    setLoading(true);
    try {
      await registerUser(values);
      message.success('注册成功，请登录');
      onRegistered();
    } catch (err) {
      message.error(err.message || '注册失败');
    } finally {
      setLoading(false);
    }
  };

  return (
    <Form onFinish={onFinish} autoComplete="off" size="large">
      <Form.Item name="username" rules={[{ required: true, message: '请输入用户名' }]}>
        <Input prefix={<UserOutlined style={{ color: '#b3bac4' }} />} placeholder="用户名" maxLength={32} />
      </Form.Item>
      <Form.Item name="nick_name" rules={[{ required: true, message: '请输入昵称' }]}>
        <Input prefix={<SmileOutlined style={{ color: '#b3bac4' }} />} placeholder="昵称" maxLength={32} />
      </Form.Item>
      <Form.Item
        name="password"
        rules={[{ required: true, message: '请输入密码' }, { min: 6, message: '密码至少 6 位' }]}
      >
        <Input.Password prefix={<LockOutlined style={{ color: '#b3bac4' }} />} placeholder="密码" maxLength={64} />
      </Form.Item>
      <Form.Item
        name="confirm"
        dependencies={['password']}
        rules={[
          { required: true, message: '请再次输入密码' },
          ({ getFieldValue }) => ({
            validator: (_, value) =>
              !value || getFieldValue('password') === value
                ? Promise.resolve()
                : Promise.reject(new Error('两次输入的密码不一致')),
          }),
        ]}
      >
        <Input.Password prefix={<LockOutlined style={{ color: '#b3bac4' }} />} placeholder="确认密码" maxLength={64} />
      </Form.Item>
      <Button type="primary" htmlType="submit" block loading={loading}>
        注 册
      </Button>
    </Form>
  );
}

export default function Login() {
  const [tab, setTab] = useState('login');

  return (
    <PageWrap>
      <BrandPanel>
        <BrandLogo>
          <BrandIcon><CloudUploadOutlined /></BrandIcon>
          <BrandTitle>AI 云存储</BrandTitle>
        </BrandLogo>
        <BrandSlogan>
          像网盘一样存储，像对话一样查找。
          <br />
          文件被自动理解、分块、向量化，语义搜索直达内容。
        </BrandSlogan>

        <FeatureItem>
          <FeatureIcon><CloudUploadOutlined /></FeatureIcon>
          <FeatureText>
            <div className="t">自动内容理解</div>
            <div className="d">上传即解析 PDF / Word / Excel / 代码等 7 类格式</div>
          </FeatureText>
        </FeatureItem>
        <FeatureItem>
          <FeatureIcon><SearchOutlined /></FeatureIcon>
          <FeatureText>
            <div className="t">语义搜索</div>
            <div className="d">描述内容而非文件名，双路召回 + LLM 重排序</div>
          </FeatureText>
        </FeatureItem>
        <FeatureItem>
          <FeatureIcon><RobotOutlined /></FeatureIcon>
          <FeatureText>
            <div className="t">AI 文件助手</div>
            <div className="d">对话式查找、分享、管理，危险操作二次确认</div>
          </FeatureText>
        </FeatureItem>
      </BrandPanel>
      <FormSide>
        <FormCard>
          <FormTitle>欢迎回来</FormTitle>
          <FormSub>登录后开始使用你的智能云盘</FormSub>
          <Tabs
            activeKey={tab}
            onChange={setTab}
            centered
            items={[
              { key: 'login', label: '登录', children: <LoginForm /> },
              {
                key: 'register',
                label: '注册',
                children: <RegisterForm onRegistered={() => setTab('login')} />,
              },
            ]}
          />
        </FormCard>
      </FormSide>
    </PageWrap>
  );
}
