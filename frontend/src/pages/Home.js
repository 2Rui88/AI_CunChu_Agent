import React, { useState } from 'react';
import {
  Card, Input, Tag, Typography, message, Empty, Spin, Space, Progress,
} from 'antd';
import {
  SearchOutlined, LinkOutlined,
} from '@ant-design/icons';
import styled from '@emotion/styled';
import { useAuth } from '../contexts/AuthContext';
import { aiSearch } from '../services/ai';
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
  const [query, setQuery] = useState('');
  const [searching, setSearching] = useState(false);
  const [results, setResults] = useState(null); // null = 未搜索

  // AI 密钥由服务端统一配置；未配置时后端返回明确错误，这里直接透出提示
  const doSearch = async (q) => {
    const text = (q ?? query).trim();
    if (!text) return;
    setQuery(text);
    setSearching(true);
    setResults([]);
    try {
      const data = await aiSearch(text, user);
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
      </HeroCard>

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
