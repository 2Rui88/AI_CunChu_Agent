import React, { useState, useEffect } from 'react';
import {
  Card, Input, Button, Tag, Typography, message, Empty, Spin, Space, Modal, Progress,
} from 'antd';
import {
  SearchOutlined, KeyOutlined, LinkOutlined, EditOutlined, CheckCircleOutlined,
} from '@ant-design/icons';
import styled from '@emotion/styled';
import { useAuth } from '../contexts/AuthContext';
import { fetchApiKey, saveApiKey, aiSearch } from '../services/ai';
import { formatFileSize, getFileIcon } from '../utils/format';

const { Text, Paragraph } = Typography;

const HeroCard = styled(Card)`
  border-radius: 16px;
  background: linear-gradient(135deg, #eff6ff 0%, #f5f3ff 60%, #ffffff 100%);
  border: 1px solid #e3eaf6;
  text-align: center;
  padding-top: 12px;
  margin-bottom: 20px;
`;

const HeroTitle = styled.h2`
  font-size: 24px;
  font-weight: 700;
  color: #1f2329;
  margin: 8px 0 6px;
`;

const HeroDesc = styled.div`
  color: #6b7280;
  font-size: 14px;
  margin-bottom: 24px;
`;

const SearchWrap = styled.div`
  max-width: 680px;
  margin: 0 auto;
`;

const Chips = styled.div`
  margin-top: 16px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: center;
`;

const Chip = styled.span`
  padding: 4px 14px;
  background: #fff;
  border: 1px solid #e3e8f0;
  border-radius: 999px;
  font-size: 13px;
  color: #4b5563;
  cursor: pointer;
  transition: all 0.2s;
  &:hover {
    border-color: #2563eb;
    color: #2563eb;
  }
`;

const KeyBar = styled.div`
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  margin-top: 18px;
  font-size: 13px;
  color: #8c95a3;
`;

const ResultCard = styled(Card)`
  border-radius: 14px;
  margin-bottom: 14px;
  transition: box-shadow 0.2s;
  &:hover {
    box-shadow: 0 4px 16px rgba(37, 99, 235, 0.08);
  }
`;

const Suggestions = ['上个月关于微服务架构的技术方案', '一张有猫的照片', 'Q2 的会议纪要'];

const sourceColor = { 向量路: 'geekblue', 关键词路: 'orange', 双路混合: 'green' };

export default function Home() {
  const { user, logout } = useAuth();
  const [apiKey, setApiKey] = useState('');
  const [keyModalOpen, setKeyModalOpen] = useState(false);
  const [keyInput, setKeyInput] = useState('');
  const [query, setQuery] = useState('');
  const [searching, setSearching] = useState(false);
  const [results, setResults] = useState(null); // null = 未搜索

  useEffect(() => {
    fetchApiKey(user).then(setApiKey);
  }, [user]);

  const handleSaveKey = async () => {
    await saveApiKey(keyInput, user);
    setApiKey(keyInput);
    setKeyModalOpen(false);
    message.success(keyInput ? 'API Key 已保存到本地浏览器' : 'API Key 已清除');
  };

  const doSearch = async (q) => {
    const text = (q ?? query).trim();
    if (!text) return;
    if (!apiKey) {
      setKeyInput('');
      setKeyModalOpen(true);
      return;
    }
    setQuery(text);
    setSearching(true);
    setResults([]);
    try {
      const data = await aiSearch(text, user, apiKey);
      setResults(data.files || []);
    } catch (err) {
      if (err.tokenExpired) {
        message.error('登录已过期，请重新登录');
        logout();
        return;
      }
      message.error(err.message || '搜索失败');
      setResults(null);
    } finally {
      setSearching(false);
    }
  };

  return (
    <div>
      <HeroCard>
        <HeroTitle>语义搜索你的文件</HeroTitle>
        <HeroDesc>描述内容而不是文件名 —— "帮我找一下上个月的技术方案"</HeroDesc>

        <SearchWrap>
          <Input.Search
            size="large"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onSearch={doSearch}
            enterButton={<span><SearchOutlined /> 搜索</span>}
            placeholder="用自然语言描述你想找的文件..."
            loading={searching}
            allowClear
          />
        </SearchWrap>

        <Chips>
          {Suggestions.map((s) => (
            <Chip key={s} onClick={() => doSearch(s)}>{s}</Chip>
          ))}
        </Chips>

        <KeyBar>
          {apiKey
            ? <CheckCircleOutlined style={{ color: '#16a34a' }} />
            : <KeyOutlined style={{ color: '#f59e0b' }} />}
          <span>DashScope API Key：{apiKey ? '已配置' : '未配置（搜索前需设置）'}</span>
          <Button type="link" size="small" icon={<EditOutlined />} onClick={() => { setKeyInput(apiKey); setKeyModalOpen(true); }}>
            {apiKey ? '修改' : '去设置'}
          </Button>
        </KeyBar>
      </HeroCard>

      <Modal
        open={keyModalOpen}
        title={<span><KeyOutlined style={{ marginRight: 8, color: '#2563eb' }} />设置 DashScope API Key</span>}
        onOk={handleSaveKey}
        onCancel={() => setKeyModalOpen(false)}
        okText="保存"
        cancelText="取消"
      >
        <Input.Password
          value={keyInput}
          onChange={(e) => setKeyInput(e.target.value)}
          placeholder="sk-...（阿里百炼 API Key）"
          size="large"
        />
        <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 10 }}>
          仅保存在本地浏览器（localStorage），用于调用 AI 描述 / 语义搜索 / AI 助手，不会存到服务器数据库。
        </Text>
      </Modal>

      {searching && (
        <div style={{ textAlign: 'center', padding: 48 }}>
          <Spin />
          <div style={{ marginTop: 12, color: '#8c95a3' }}>正在理解查询、双路召回并重排序...</div>
        </div>
      )}

      {!searching && results !== null && results.length === 0 && (
        <Card style={{ borderRadius: 14 }}>
          <Empty description={<span>没有找到相关文件<br /><Text type="secondary" style={{ fontSize: 13 }}>试试换个描述方式，或先上传一些文件</Text></span>} />
        </Card>
      )}

      {!searching && results !== null && results.length > 0 && (
        <>
          <div style={{ margin: '4px 0 14px', color: '#6b7280', fontSize: 13 }}>
            找到 <b style={{ color: '#2563eb' }}>{results.length}</b> 个相关文件
          </div>
          {results.map((f, idx) => (
            <ResultCard key={`${f.md5}_${f.chunk_index ?? 0}_${idx}`}>
              <Space align="start" size={14} style={{ width: '100%' }}>
                <div style={{ fontSize: 26, paddingTop: 2 }}>{getFileIcon(f.type, { fontSize: 26 })}</div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <Space wrap style={{ marginBottom: 4 }}>
                    <a href={f.url} target="_blank" rel="noreferrer" style={{ fontSize: 15, fontWeight: 600 }}>
                      {f.filename || f.file_name}
                      <LinkOutlined style={{ marginLeft: 6, fontSize: 12 }} />
                    </a>
                    {f.match_context && <Tag color="blue" bordered={false}>{f.match_context}</Tag>}
                    {f.source && <Tag color={sourceColor[f.source] || 'default'} bordered={false}>{f.source}</Tag>}
                  </Space>
                  <div style={{ fontSize: 12.5, color: '#8c95a3', marginBottom: 8 }}>
                    {String(f.type || '').toUpperCase() || '未知类型'} · {formatFileSize(f.size)}
                    {f.score !== undefined && f.score > 0 && (
                      <span style={{ marginLeft: 12 }}>相似度 {(f.score * 100).toFixed(1)}%</span>
                    )}
                  </div>
                  {f.description && (
                    <Paragraph ellipsis={{ rows: 3, expandable: true, symbol: '展开' }} style={{ marginBottom: 0, color: '#4b5563' }}>
                      {f.description}
                    </Paragraph>
                  )}
                </div>
                {f.score !== undefined && f.score > 0 && (
                  <Progress
                    type="circle"
                    size={44}
                    percent={Math.min(100, Math.round(f.score * 100))}
                    strokeColor="#2563eb"
                    format={() => `${(f.score * 100).toFixed(0)}`}
                  />
                )}
              </Space>
            </ResultCard>
          ))}
        </>
      )}
    </div>
  );
}
