import React from 'react';
import { Modal, Typography, Descriptions, Tag } from 'antd';
import { ExclamationCircleOutlined } from '@ant-design/icons';

const { Text } = Typography;

/** 危险操作确认弹窗 */
export default function ConfirmModal({ open, data, onConfirm, onCancel }) {
  if (!data) return null;

  const isDelete = data.tool_name === 'delete_file';
  const fileInfo = data.file_info || {};

  const formatSize = (bytes) => {
    if (!bytes) return '';
    const n = parseInt(bytes, 10);
    if (n > 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
    if (n > 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${n} B`;
  };

  return (
    <Modal
      open={open}
      title={
        <span>
          <ExclamationCircleOutlined style={{ color: '#faad14', marginRight: 8 }} />
          操作确认
        </span>
      }
      onOk={() => onConfirm(data.confirmation_token, 'approved')}
      onCancel={() => onCancel(data.confirmation_token, 'rejected')}
      okText={isDelete ? '确认删除' : '确认分享'}
      cancelText="取消"
      okButtonProps={{ danger: isDelete }}
      width={460}
    >
      <Text style={{ fontSize: 15 }}>{data.message}</Text>

      {fileInfo.filename && (
        <Descriptions size="small" column={1} bordered style={{ marginTop: 12 }}>
          <Descriptions.Item label="文件名">{fileInfo.filename}</Descriptions.Item>
          {fileInfo.size && <Descriptions.Item label="大小">{formatSize(fileInfo.size)}</Descriptions.Item>}
          {fileInfo.type && <Descriptions.Item label="类型">{fileInfo.type}</Descriptions.Item>}
        </Descriptions>
      )}

      <div style={{ marginTop: 12 }}>
        <Tag color={isDelete ? 'red' : 'blue'}>
          {isDelete ? '此操作不可撤销' : '文件将对所有用户可见'}
        </Tag>
      </div>
    </Modal>
  );
}
