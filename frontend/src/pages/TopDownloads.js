import React, { useState, useEffect } from 'react';
import { Card, Button, List, Spin, Empty, Typography } from 'antd';
import { TrophyOutlined, ReloadOutlined, DownloadOutlined, FireOutlined } from '@ant-design/icons';
import styled from '@emotion/styled';
import { fetchTopDownloads } from '../services/images';

const { Text } = Typography;

const WrapCard = styled(Card)`
  border-radius: 14px;
`;

const Podium = styled.div`
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 16px;
  margin-bottom: 20px;
  @media (max-width: 640px) {
    grid-template-columns: 1fr;
  }
`;

const PodiumCard = styled.div`
  border-radius: 14px;
  padding: 20px;
  text-align: center;
  border: 1px solid #eef0f4;
  background: ${(p) => p.$bg};
  position: relative;
  .medal {
    font-size: 34px;
    line-height: 1;
  }
  .name {
    margin-top: 10px;
    font-size: 14px;
    font-weight: 600;
    color: #1f2329;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .count {
    margin-top: 6px;
    font-size: 24px;
    font-weight: 700;
    color: #2563eb;
  }
  .sub {
    font-size: 12px;
    color: #8c95a3;
  }
`;

const RankItem = styled.div`
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 12px 4px;
  border-bottom: 1px solid #f2f4f8;
  &:last-child {
    border-bottom: none;
  }
`;

const RankNum = styled.div`
  width: 28px;
  height: 28px;
  border-radius: 8px;
  background: #f2f4f8;
  color: #6b7280;
  font-size: 13px;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
  flex: none;
`;

const MedalMeta = [
  { medal: '🥇', bg: 'linear-gradient(160deg, #fffbeb, #fef3c7)', label: '冠军' },
  { medal: '🥈', bg: 'linear-gradient(160deg, #f8fafc, #f1f5f9)', label: '亚军' },
  { medal: '🥉', bg: 'linear-gradient(160deg, #fff7ed, #ffedd5)', label: '季军' },
];

export default function TopDownloads() {
  const [files, setFiles] = useState([]);
  const [loading, setLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      setFiles(await fetchTopDownloads());
    } catch {
      setFiles([]);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const top3 = files.slice(0, 3);
  const rest = files.slice(3);

  return (
    <WrapCard
      title={(
        <span>
          <TrophyOutlined style={{ color: '#f59e0b', marginRight: 8 }} />
          下载排行榜
          <Text type="secondary" style={{ fontSize: 13, fontWeight: 400, marginLeft: 10 }}>
            按全站下载量排序的共享文件
          </Text>
        </span>
      )}
      extra={(
        <Button icon={<ReloadOutlined />} onClick={load} loading={loading}>
          刷新
        </Button>
      )}
    >
      {loading ? (
        <div style={{ textAlign: 'center', padding: 60 }}><Spin /></div>
      ) : files.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无下载数据" style={{ padding: 40 }} />
      ) : (
        <>
          <Podium>
            {top3.map((f, i) => (
              <PodiumCard key={f.fileid} $bg={MedalMeta[i].bg}>
                <div className="medal">{MedalMeta[i].medal}</div>
                <div className="name" title={f.file_name}>{f.file_name}</div>
                <div className="count">{f.pv || 0}</div>
                <div className="sub">次下载 · {MedalMeta[i].label}</div>
              </PodiumCard>
            ))}
          </Podium>

          {rest.length > 0 && (
            <List
              dataSource={rest}
              split={false}
              renderItem={(f, i) => (
                <RankItem>
                  <RankNum>{i + 4}</RankNum>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{
                      fontSize: 14, fontWeight: 500, color: '#1f2329',
                      whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                    }}
                    >
                      {f.file_name}
                    </div>
                  </div>
                  <Text type="secondary" style={{ fontSize: 13 }}>
                    <DownloadOutlined style={{ marginRight: 4 }} />
                    {f.pv || 0} 次
                  </Text>
                </RankItem>
              )}
            />
          )}

          {files.length === 0 && null}
          <div style={{ textAlign: 'center', marginTop: 12, fontSize: 12, color: '#b3bac4' }}>
            <FireOutlined /> 数据来自共享文件，实时更新
          </div>
        </>
      )}
    </WrapCard>
  );
}
