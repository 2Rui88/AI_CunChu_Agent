import React from 'react';
import { Modal, Typography, Descriptions, Tag } from 'antd';
import {
  ExclamationCircleOutlined, DeleteOutlined, ShareAltOutlined,
} from '@ant-design/icons';
import { formatFileSize } from '../utils/format';

const { Text } = Typography;

/** 危险操作确认弹窗（删除/分享） */
export default function ConfirmModal({ open, data, onConfirm, onCancel }) {
  if (!data) return null;

  const isDelete = data.tool_name === 'delete_file';
  const fileInfo = data.file_info || {};

  return (
    <Modal
      open={open}
      title={
        <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          {isDelete
            ? <DeleteOutlined style={{ color: '#ef4444' }} />
            : <ShareAltOutlined style={{ color: '#2563eb' }} />}
          {isDelete ? '确认删除文件' : '确认分享文件'}
        </span>
      }
      onOk={() => onConfirm(data.confirmation_token, 'approved')}
      onCancel={() => onCancel(data.confirmation_token, 'rejected')}
      okText={isDelete ? '删除' : '分享'}
      cancelText="取消"
      okButtonProps={{ danger: isDelete, type: isDelete ? 'primary' : 'primary' }}
      width={440}
    >
      <div
        style={{
          background: isDelete ? '#fef2f2' : '#eff6ff',
          borderRadius: 8,
          padding: '10px 12px',
          marginBottom: 12,
          display: 'flex',
          alignItems: 'center',
          gap: 8,
        }}
      >
        <ExclamationCircleOutlined style={{ color: isDelete ? '#ef4444' : '#2563eb', fontSize: 18 }} />
        <Text style={{ fontSize: 14 }}>{data.message}</Text>
      </div>

      {fileInfo.filename && (
        <Descriptions size="small" column={1} bordered>
          <Descriptions.Item label="文件名">{fileInfo.filename}</Descriptions.Item>
          {fileInfo.size !== undefined && fileInfo.size !== '' && (
            <Descriptions.Item label="大小">{formatFileSize(fileInfo.size)}</Descriptions.Item>
          )}
          {fileInfo.type && (
            <Descriptions.Item label="类型">{String(fileInfo.type).toUpperCase()}</Descriptions.Item>
          )}
        </Descriptions>
      )}

      <div style={{ marginTop: 12 }}>
        <Tag color={isDelete ? 'red' : 'blue'} bordered={false}>
          {isDelete ? '此操作不可撤销' : '文件将对所有用户可见'}
        </Tag>
        <Text type="secondary" style={{ fontSize: 12, marginLeft: 4 }}>
          5 分钟内未确认将自动取消
        </Text>
      </div>
    </Modal>
  );
}
