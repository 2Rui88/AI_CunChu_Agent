import React, { useState, useEffect, useRef, useMemo } from 'react';
import {
  Card, Table, message, Button, Tooltip, Space, Input, Empty, Drawer,
  Tabs, Tag, Descriptions, Typography, Select, InputNumber, Progress,
  Upload, Collapse, Popconfirm, Form,
} from 'antd';
import {
  ReloadOutlined, DeleteOutlined, PlayCircleOutlined, StopOutlined,
  ExperimentOutlined, FileTextOutlined, UploadOutlined, EyeOutlined,
  RightCircleOutlined,
} from '@ant-design/icons';
import styled from '@emotion/styled';
import { useAuth } from '../contexts/AuthContext';
import {
  uploadDataset, fetchDatasets, fetchDatasetItems, deleteDataset,
  startRun, stepRun, fetchRuns, fetchRunDetail, deleteRun,
} from '../services/eval';

const ListCard = styled(Card)`
  border-radius: 14px;
`;

const StatCard = styled(Card)`
  border-radius: 12px;
  text-align: center;
  .num {
    font-size: 22px;
    font-weight: 700;
    color: #2563eb;
  }
  .label {
    font-size: 12.5px;
    color: #8c95a3;
    margin-top: 2px;
  }
`;

const MetricRow = styled.div`
  display: flex;
  gap: 16px;
  margin-bottom: 16px;
`;

const FeedItem = styled.div`
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border: 1px solid #eef0f4;
  border-radius: 10px;
  margin-bottom: 8px;
  background: #fbfcfe;
  font-size: 13px;
`;

const JSONL_EXAMPLE = [
  '{"query": "微服务架构的技术方案在哪份文档里？", "gold_files": ["2025年度技术方案.docx"]}',
  '{"query": "数据平台 Q3 的建设目标", "gold_md5s": ["d41d8cd98f00b204e9800998ecf8427e"], "gold_answer": "……（可选，留待答案评估）"}',
].join('\n');

const fmtMetric = (v) => (v === null || v === undefined ? '-' : Number(v).toFixed(4));

/** 指标卡组：聚合结果里存在哪些档位就展示哪些 */
const MetricCards = ({ metrics }) => {
  const entries = [
    ['recall@1', 'Recall@1'], ['recall@5', 'Recall@5'], ['recall@10', 'Recall@10'],
    ['mrr', 'MRR'],
  ].filter(([key]) => metrics[key] !== undefined);
  if (!entries.length) return null;
  return (
    <MetricRow>
      {entries.map(([key, label]) => (
        <StatCard key={key} style={{ flex: 1 }}>
          <div className="num">{fmtMetric(metrics[key])}</div>
          <div className="label">{label}</div>
        </StatCard>
      ))}
    </MetricRow>
  );
};

const EvalCenter = () => {
  const { user, logout } = useAuth();
  const [activeTab, setActiveTab] = useState('datasets');
  const [datasets, setDatasets] = useState([]);
  const [datasetsLoading, setDatasetsLoading] = useState(false);
  const [uploadForm] = Form.useForm();

  // 评估运行：历史 + 进行中的循环状态（循环由前端驱动逐题调 /eval/run/step）
  const [runs, setRuns] = useState([]);
  const [runsLoading, setRunsLoading] = useState(false);
  const [selectedDataset, setSelectedDataset] = useState(null);
  const [topK, setTopK] = useState(10);
  const [live, setLive] = useState({
    running: false, runId: null, completed: 0, total: 0, items: [], metrics: null,
  });
  const runLoopRef = useRef(false);

  // 题目明细抽屉（数据集）
  const [itemsDrawer, setItemsDrawer] = useState({ open: false, loading: false, name: '', items: [] });
  // 运行详情抽屉
  const [runDrawer, setRunDrawer] = useState({
    open: false, loading: false, run: null, items: [], filter: 'all',
  });

  const handleExpired = (error) => {
    message.error(error.message || '登录已过期，请重新登录');
    logout();
  };

  const wrapError = (fallback) => (error) => {
    if (error.tokenExpired) { handleExpired(error); return true; }
    message.error(error.message || fallback);
    return false;
  };

  const refreshDatasets = async () => {
    setDatasetsLoading(true);
    try {
      setDatasets(await fetchDatasets(user));
    } catch (error) {
      wrapError('获取数据集列表失败')(error);
    } finally {
      setDatasetsLoading(false);
    }
  };

  const refreshRuns = async () => {
    setRunsLoading(true);
    try {
      setRuns(await fetchRuns(user));
    } catch (error) {
      wrapError('获取运行列表失败')(error);
    } finally {
      setRunsLoading(false);
    }
  };

  useEffect(() => {
    if (user && user.token) {
      refreshDatasets();
      refreshRuns();
    }
  }, [user]);

  // ---------- 数据集：上传 / 查看 / 删除 ----------

  const submitUpload = async ({ name, description, content }) => {
    if (!content || !content.trim()) {
      message.warning('请填写 JSONL 题目内容或选择 .jsonl 文件');
      return;
    }
    try {
      const data = await uploadDataset(user, name, description, content);
      message.success(`已导入 ${data.item_count} 道题（数据集 ${data.dataset_id}）`);
      uploadForm.resetFields();
      refreshDatasets();
    } catch (error) {
      wrapError('上传失败')(error);
    }
  };

  const openItems = async (record) => {
    setItemsDrawer({ open: true, loading: true, name: record.name, items: [] });
    try {
      const items = await fetchDatasetItems(user, record.dataset_id);
      setItemsDrawer({ open: true, loading: false, name: record.name, items });
    } catch (error) {
      setItemsDrawer((d) => ({ ...d, loading: false }));
      wrapError('获取题目失败')(error);
    }
  };

  const confirmDeleteDataset = async (record) => {
    try {
      await deleteDataset(user, record.dataset_id);
      message.success('数据集已删除');
      refreshDatasets();
      refreshRuns();
    } catch (error) {
      wrapError('删除失败')(error);
    }
  };

  // ---------- 评估循环：前端驱动逐题推进 ----------

  const driveLoop = async (runId) => {
    runLoopRef.current = true;
    setLive({ running: true, runId, completed: 0, total: 0, items: [], metrics: null });
    try {
      while (runLoopRef.current) {
        const step = await stepRun(user, runId);
        setLive((s) => ({
          ...s,
          completed: step.completed,
          total: step.total,
          items: step.item ? [step.item, ...s.items] : s.items,
          metrics: step.done ? step.metrics : null,
        }));
        if (step.done) {
          message.success('评估完成');
          break;
        }
      }
    } catch (error) {
      if (!error.tokenExpired) {
        message.error(`${error.message || '评估中断'}——可稍后续跑`);
      } else {
        handleExpired(error);
        return;
      }
    } finally {
      runLoopRef.current = false;
      setLive((s) => ({ ...s, running: false }));
      refreshRuns();
    }
  };

  const startEval = async () => {
    if (!selectedDataset) {
      message.warning('请选择要评估的数据集');
      return;
    }
    try {
      const { run_id: runId } = await startRun(user, selectedDataset, topK);
      await driveLoop(runId);
    } catch (error) {
      wrapError('创建评估失败')(error);
    }
  };

  const stopEval = () => {
    runLoopRef.current = false;
    message.info('已停止推进（已完成的题目不会丢失，可随时续跑）');
  };

  // ---------- 运行详情 ----------

  const openRun = async (record) => {
    setRunDrawer({ open: true, loading: true, run: null, items: [], filter: 'all' });
    try {
      const data = await fetchRunDetail(user, record.run_id);
      setRunDrawer({ open: true, loading: false, run: data, items: data.items || [], filter: 'all' });
    } catch (error) {
      setRunDrawer((d) => ({ ...d, loading: false }));
      wrapError('获取运行详情失败')(error);
    }
  };

  const confirmDeleteRun = async (record) => {
    try {
      await deleteRun(user, record.run_id);
      message.success('运行记录已删除');
      refreshRuns();
    } catch (error) {
      wrapError('删除失败')(error);
    }
  };

  // ---------- 渲染辅助 ----------

  const datasetColumns = [
    { title: '名称', dataIndex: 'name', key: 'name', render: (t) => <span style={{ fontWeight: 500 }}>{t}</span> },
    {
      title: '题数', dataIndex: 'item_count', key: 'item_count', width: 80, align: 'center',
    },
    {
      title: '金标', key: 'gold', width: 150, align: 'center',
      render: (_, r) => (
        <>
          {r.has_gold ? <Tag color="green">文件金标</Tag> : <Tag>无金标</Tag>}
          {r.has_gold_answer && <Tag color="blue">参考答案</Tag>}
        </>
      ),
    },
    { title: '创建时间', dataIndex: 'create_time', key: 'create_time', width: 170 },
    {
      title: '操作', key: 'action', width: 230,
      render: (_, r) => (
        <Space size={4}>
          <Button type="link" size="small" icon={<EyeOutlined />} onClick={() => openItems(r)}>
            题目
          </Button>
          <Button
            type="link" size="small" icon={<PlayCircleOutlined />}
            onClick={() => { setSelectedDataset(r.dataset_id); setActiveTab('runs'); }}
          >
            去评估
          </Button>
          <Popconfirm title="删除数据集将连同其全部评估记录，确定？" onConfirm={() => confirmDeleteDataset(r)}>
            <Button type="link" size="small" danger icon={<DeleteOutlined />}>删除</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const runColumns = [
    { title: '运行', dataIndex: 'name', key: 'name', render: (t) => <span style={{ fontWeight: 500 }}>{t}</span> },
    {
      title: '状态', dataIndex: 'status', key: 'status', width: 100, align: 'center',
      render: (s) => (
        <Tag color={s === 'completed' ? 'green' : 'processing'}>
          {s === 'completed' ? '已完成' : '进行中'}
        </Tag>
      ),
    },
    {
      title: '进度', key: 'progress', width: 130, align: 'center',
      render: (_, r) => `${r.completed_items}/${r.total_items}`,
    },
    {
      title: 'Recall@10', key: 'r10', width: 100, align: 'center',
      render: (_, r) => fmtMetric(r.metrics?.['recall@10']),
    },
    { title: 'MRR', key: 'mrr', width: 90, align: 'center', render: (_, r) => fmtMetric(r.metrics?.mrr) },
    { title: '创建时间', dataIndex: 'create_time', key: 'create_time', width: 170 },
    {
      title: '操作', key: 'action', width: 210,
      render: (_, r) => (
        <Space size={4}>
          <Button type="link" size="small" icon={<EyeOutlined />} onClick={() => openRun(r)}>详情</Button>
          {r.status === 'running' && (
            <Tooltip title="从中断处继续逐题推进">
              <Button
                type="link" size="small" icon={<RightCircleOutlined />}
                disabled={live.running}
                onClick={() => driveLoop(r.run_id)}
              >
                续跑
              </Button>
            </Tooltip>
          )}
          <Popconfirm title="删除该运行记录？" onConfirm={() => confirmDeleteRun(r)}>
            <Button type="link" size="small" danger icon={<DeleteOutlined />}>删除</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const hitTag = (item) => {
    if (item.error) return <Tag color="red">执行失败</Tag>;
    if (!item.gold_md5s?.length) return <Tag>无金标</Tag>;
    const rank = item.metrics?.first_hit_rank;
    if (rank === null || rank === undefined) return <Tag color="red">未命中</Tag>;
    return <Tag color={rank <= 3 ? 'green' : 'orange'}>第 {rank} 位</Tag>;
  };

  const runItemColumns = [
    { title: '#', dataIndex: 'item_index', key: 'item_index', width: 50 },
    {
      title: '问题', dataIndex: 'query', key: 'query',
      render: (t) => <Typography.Text style={{ maxWidth: 260 }} ellipsis={{ tooltip: t }}>{t}</Typography.Text>,
    },
    {
      title: '金标文件', dataIndex: 'gold_names', key: 'gold_names', width: 180,
      render: (names) => (names?.length
        ? names.slice(0, 2).map((n, i) => (
          <Tag key={i} style={{ maxWidth: 120, overflow: 'hidden', textOverflow: 'ellipsis' }}>{n || '(md5)'}</Tag>
        ))
        : <Tag>无</Tag>),
    },
    { title: '首中', key: 'hit', width: 90, align: 'center', render: (_, item) => hitTag(item) },
    {
      title: 'Recall@5', key: 'r5', width: 95, align: 'center',
      render: (_, item) => fmtMetric(item.metrics?.['recall@5']),
    },
    { title: 'MRR', key: 'mrr', width: 85, align: 'center', render: (_, item) => fmtMetric(item.metrics?.mrr) },
  ];

  const retrievedColumns = [
    { title: '排名', dataIndex: 'rank', key: 'rank', width: 60, align: 'center' },
    { title: '文件名', dataIndex: 'filename', key: 'filename' },
    { title: '来源', dataIndex: 'source', key: 'source', width: 90 },
    { title: '向量位', dataIndex: 'vector_rank', key: 'vector_rank', width: 75, align: 'center', render: (v) => v ?? '-' },
    { title: '词区位', dataIndex: 'keyword_rank', key: 'keyword_rank', width: 75, align: 'center', render: (v) => v ?? '-' },
    { title: 'RRF', dataIndex: 'rrf_score', key: 'rrf_score', width: 80, align: 'center' },
    { title: '命中位置', dataIndex: 'match_context', key: 'match_context', width: 180, ellipsis: true },
  ];

  const filteredRunItems = useMemo(() => {
    const items = runDrawer.items;
    if (runDrawer.filter === 'miss') {
      return items.filter((it) => it.gold_md5s?.length && !it.error
        && (it.metrics?.first_hit_rank === null || it.metrics?.first_hit_rank === undefined));
    }
    if (runDrawer.filter === 'error') return items.filter((it) => it.error);
    return items;
  }, [runDrawer.items, runDrawer.filter]);

  const { run: drawerRun } = runDrawer;

  return (
    <div>
      <Tabs
        activeKey={activeTab}
        onChange={setActiveTab}
        items={[
          {
            key: 'datasets',
            label: (
              <span>
                <FileTextOutlined /> 评估数据集
              </span>
            ),
            children: (
              <div>
                <ListCard style={{ marginBottom: 16 }}>
                  <div style={{ fontWeight: 600, marginBottom: 12 }}>上传数据集（JSONL，每行一题）</div>
                  <Form form={uploadForm} layout="vertical" onFinish={submitUpload}>
                    <Space style={{ display: 'flex' }} align="start">
                      <Form.Item name="name" style={{ width: 220, minWidth: 220 }} label="名称">
                        <Input placeholder="如：检索回归集 v1" maxLength={60} />
                      </Form.Item>
                      <Form.Item name="description" style={{ flex: 1 }} label="说明（可选）">
                        <Input placeholder="数据集用途备注" maxLength={200} />
                      </Form.Item>
                    </Space>
                    <Form.Item
                      name="content" label="题目内容"
                      extra={(
                        <Upload
                          accept=".jsonl,.json,.txt"
                          maxCount={1}
                          showUploadList={false}
                          beforeUpload={async (file) => {
                            const text = await file.text();
                            uploadForm.setFieldValue('content', text);
                            if (!uploadForm.getFieldValue('name')) {
                              uploadForm.setFieldValue('name', file.name.replace(/\.(jsonl|json|txt)$/i, ''));
                            }
                            message.success(`已读取 ${file.name}`);
                            return false;
                          }}
                        >
                          <Button size="small" icon={<UploadOutlined />}>选择 .jsonl 文件</Button>
                        </Upload>
                      )}
                    >
                      <Input.TextArea
                        rows={6}
                        style={{ fontFamily: 'monospace', fontSize: 12 }}
                        placeholder={JSONL_EXAMPLE}
                      />
                    </Form.Item>
                    <Space>
                      <Button type="primary" htmlType="submit" icon={<UploadOutlined />}>导入</Button>
                    </Space>
                  </Form>
                  <Collapse
                    size="small"
                    style={{ marginTop: 12 }}
                    items={[{
                      key: 'help',
                      label: '格式说明',
                      children: (
                        <div style={{ fontSize: 12.5, color: '#4b5563', lineHeight: 1.9 }}>
                          <div>每行一个 JSON 对象：<code>query</code> 必填；<code>gold_files</code>（文件名，须在你的文件列表中存在，同名全部视为金标）与 <code>gold_md5s</code>（可从「向量化文档」页获取）至少其一建议提供；<code>gold_answer</code> 可选（当前阶段仅存储）。</div>
                          <div>无金标的题只执行检索不参与指标聚合；gold_files 解析不到任何文件时整个上传会被拒绝。</div>
                          <pre style={{ background: '#f6f8fa', padding: 10, borderRadius: 8, fontSize: 12 }}>{JSONL_EXAMPLE}</pre>
                        </div>
                      ),
                    }]}
                  />
                </ListCard>

                <ListCard>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
                    <Space>
                      <span style={{ fontWeight: 600 }}>我的数据集</span>
                      <Tooltip title="刷新列表">
                        <Button type="text" icon={<ReloadOutlined />} onClick={refreshDatasets} loading={datasetsLoading} />
                      </Tooltip>
                    </Space>
                  </div>
                  <Table
                    columns={datasetColumns}
                    dataSource={datasets}
                    rowKey="dataset_id"
                    loading={datasetsLoading}
                    pagination={{ pageSize: 10, showTotal: (t) => `共 ${t} 个数据集` }}
                    locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无数据集，先上传一份 JSONL" /> }}
                  />
                </ListCard>
              </div>
            ),
          },
          {
            key: 'runs',
            label: (
              <span>
                <ExperimentOutlined /> 评估运行
              </span>
            ),
            children: (
              <div>
                <ListCard style={{ marginBottom: 16 }}>
                  <div style={{ fontWeight: 600, marginBottom: 12 }}>发起评估</div>
                  <Space wrap>
                    <Select
                      style={{ width: 280 }}
                      placeholder="选择数据集"
                      value={selectedDataset}
                      onChange={setSelectedDataset}
                      options={datasets.map((d) => ({
                        value: d.dataset_id, label: `${d.name}（${d.item_count} 题${d.has_gold ? '' : '，无金标'}）`,
                      }))}
                    />
                    <Tooltip title="单题检索返回的候选数（指标档位 K ≤ top_k）">
                      <Space size={6}>
                        <span style={{ color: '#8c95a3', fontSize: 13 }}>top_k</span>
                        <InputNumber min={1} max={20} value={topK} onChange={(v) => setTopK(v || 10)} style={{ width: 72 }} />
                      </Space>
                    </Tooltip>
                    {live.running ? (
                      <Button danger icon={<StopOutlined />} onClick={stopEval}>停止</Button>
                    ) : (
                      <Button type="primary" icon={<PlayCircleOutlined />} onClick={startEval}>
                        开始评估
                      </Button>
                    )}
                  </Space>

                  {(live.running || live.items.length > 0) && (
                    <div style={{ marginTop: 16 }}>
                      <Progress
                        percent={live.total ? Math.round((live.completed / live.total) * 100) : 0}
                        status={live.running ? 'active' : 'success'}
                        format={() => `${live.completed}/${live.total || '?'} 题`}
                      />
                      <div style={{ maxHeight: 260, overflowY: 'auto', marginTop: 10 }}>
                        {live.items.slice(0, 20).map((item) => (
                          <FeedItem key={item.item_index}>
                            <Tag style={{ marginRight: 0 }}>{item.item_index}</Tag>
                            <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                              {item.query}
                            </span>
                            {hitTag(item)}
                          </FeedItem>
                        ))}
                      </div>
                    </div>
                  )}
                  {live.metrics && !live.running && (
                    <div style={{ marginTop: 8 }}>
                      <MetricCards metrics={live.metrics} />
                    </div>
                  )}
                </ListCard>

                <ListCard>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
                    <Space>
                      <span style={{ fontWeight: 600 }}>运行历史</span>
                      <Tooltip title="刷新列表">
                        <Button type="text" icon={<ReloadOutlined />} onClick={refreshRuns} loading={runsLoading} />
                      </Tooltip>
                    </Space>
                  </div>
                  <Table
                    columns={runColumns}
                    dataSource={runs}
                    rowKey="run_id"
                    loading={runsLoading}
                    pagination={{ pageSize: 10, showTotal: (t) => `共 ${t} 次运行` }}
                    locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无评估运行" /> }}
                  />
                </ListCard>
              </div>
            ),
          },
        ]}
      />

      {/* 数据集题目抽屉 */}
      <Drawer
        open={itemsDrawer.open}
        onClose={() => setItemsDrawer({ open: false, loading: false, name: '', items: [] })}
        width={640}
        title={(
          <Space>
            <FileTextOutlined style={{ color: '#2563eb' }} />
            <span>{itemsDrawer.name || '题目明细'}</span>
          </Space>
        )}
      >
        {itemsDrawer.loading ? (
          <div style={{ textAlign: 'center', padding: '80px 0' }}>加载中...</div>
        ) : (
          <Table
            size="small"
            columns={[
              { title: '#', dataIndex: 'item_index', key: 'item_index', width: 50 },
              { title: '问题', dataIndex: 'query', key: 'query' },
              {
                title: '金标', key: 'gold', width: 180,
                render: (_, it) => (it.gold_names?.length ? it.gold_names.join('、') : '无'),
              },
            ]}
            dataSource={itemsDrawer.items}
            rowKey="item_index"
            pagination={{ pageSize: 15 }}
          />
        )}
      </Drawer>

      {/* 运行详情抽屉 */}
      <Drawer
        open={runDrawer.open}
        onClose={() => setRunDrawer({ open: false, loading: false, run: null, items: [], filter: 'all' })}
        width={860}
        title={(
          <Space>
            <ExperimentOutlined style={{ color: '#2563eb' }} />
            <span>{drawerRun?.name || '评估详情'}</span>
          </Space>
        )}
      >
        {runDrawer.loading ? (
          <div style={{ textAlign: 'center', padding: '80px 0' }}>加载中...</div>
        ) : drawerRun && (
          <>
            {drawerRun.status === 'completed' && <MetricCards metrics={drawerRun.metrics || {}} />}
            <Descriptions
              size="small" column={2} bordered style={{ marginBottom: 16 }}
              items={[
                { key: 'status', label: '状态', children: drawerRun.status === 'completed' ? '已完成' : '进行中（可续跑）' },
                { key: 'progress', label: '进度', children: `${drawerRun.completed_items}/${drawerRun.total_items}` },
                { key: 'models', label: '模型', children: `${drawerRun.config?.chat_model || '-'} / ${drawerRun.config?.embedding_model || '-'}` },
                { key: 'topk', label: 'top_k', children: drawerRun.top_k },
                { key: 'score_th', label: '分数阈值', children: drawerRun.config?.score_threshold ?? '-' },
                { key: 'rerank', label: '重排触发', children: `候选 > ${drawerRun.config?.rerank_trigger ?? '-'} 取前 ${drawerRun.config?.rerank_candidates ?? '-'} 精排` },
                {
                  key: 'stat', label: '有效/失败题数',
                  children: `${drawerRun.metrics?.evaluated ?? '-'} / ${drawerRun.metrics?.errors ?? 0}`,
                },
                { key: 'time', label: '时间', children: `${drawerRun.create_time || '-'} ~ ${drawerRun.finish_time || '-'}` },
              ]}
            />
            <div style={{ marginBottom: 10 }}>
              <Space>
                <span style={{ fontWeight: 600 }}>逐题结果</span>
                <Select
                  size="small" style={{ width: 130 }}
                  value={runDrawer.filter}
                  onChange={(v) => setRunDrawer((d) => ({ ...d, filter: v }))}
                  options={[
                    { value: 'all', label: '全部' },
                    { value: 'miss', label: '未命中' },
                    { value: 'error', label: '执行失败' },
                  ]}
                />
              </Space>
            </div>
            <Table
              size="small"
              columns={runItemColumns}
              dataSource={filteredRunItems}
              rowKey="item_index"
              pagination={{ pageSize: 15, showTotal: (t) => `${t} 题` }}
              expandable={{
                expandedRowRender: (item) => (item.error
                  ? <Typography.Text type="danger" style={{ fontSize: 12 }}>{item.error}</Typography.Text>
                  : (
                    <Table
                      size="small" columns={retrievedColumns} dataSource={item.retrieved || []}
                      rowKey="rank" pagination={false}
                    />
                  )),
              }}
              locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无匹配题目" /> }}
            />
          </>
        )}
      </Drawer>
    </div>
  );
};

export default EvalCenter;
