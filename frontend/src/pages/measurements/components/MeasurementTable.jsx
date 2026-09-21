import { useState } from 'react'
import DataTable from '../../../components/common/DataTable.jsx'
import Modal from '../../../components/common/Modal.jsx'
import { Field, Input } from '../../../components/common/FormField.jsx'
import { Alert } from '../../../components/common/Feedback.jsx'
import Tag from '../../../components/common/Tag.jsx'
import { DATA_SOURCE_TONE } from '../../../constants/index.js'
import { useValuePolicy } from '../../../hooks/useValuePolicy.js'
import { formatDateTime, formatNumber, formatRatio } from '../../../utils/format.js'
import { maxFor, validateReading } from '../../../utils/validation.js'

function CorrectDialog({ row, onClose, onCorrect }) {
  const { policy } = useValuePolicy()
  const [value, setValue] = useState(row?.value ?? '')
  const [note, setNote] = useState('')
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  if (!row) return null
  const fieldError = validateReading(value, row.pollutant, policy)

  const submit = async () => {
    if (fieldError) {
      setError(fieldError)
      return
    }
    setBusy(true)
    setError(null)
    try {
      await onCorrect(row, { value: Number(value), note: note || undefined })
      onClose()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={Boolean(row)}
      title={`修正异常数据 · ${row.pollutant_label}`}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            取消
          </button>
          <button type="button" className="btn btn-primary" onClick={submit} disabled={busy || Boolean(fieldError)}>
            {busy ? '提交中...' : '提交修正'}
          </button>
        </>
      }
    >
      <div className="stack">
        {row.is_valid ? null : (
          <Alert tone="warning">
            系统标记的异常原因: {row.invalid_reason || '未通过合理性校验'}。修正后超标判定、
            达标率与站点排名会按同一口径立即重算。
          </Alert>
        )}
        <div className="small muted">
          {formatDateTime(row.measured_at)} · {row.station?.name || ''} · {row.period_label} ·
          可信量程 0 ~ {maxFor(policy, row.pollutant)} {row.unit}
        </div>
        <Field label="正确的监测值" required error={fieldError}>
          <Input
            type="number"
            step="0.01"
            min={policy.min_value}
            max={maxFor(policy, row.pollutant)}
            value={value}
            onChange={(event) => setValue(event.target.value)}
            invalid={Boolean(fieldError)}
            autoFocus
          />
        </Field>
        <Field label="修正说明">
          <Input value={note} onChange={(event) => setNote(event.target.value)} placeholder="如: 仪器负数, 经台账复核" />
        </Field>
        {error ? <Alert tone="error">{error}</Alert> : null}
      </div>
    </Modal>
  )
}

export default function MeasurementTable({ rows, loading, onDelete, onCorrect }) {
  const [correcting, setCorrecting] = useState(null)

  const columns = [
    { key: 'measured_at', title: '监测时间', className: 'cell-nowrap', render: (row) => formatDateTime(row.measured_at) },
    {
      key: 'station',
      title: '监测点',
      render: (row) => (
        <div>
          <div>{row.station?.name || '-'}</div>
          <div className="small muted mono">{row.station?.code || ''}</div>
        </div>
      )
    },
    { key: 'pollutant_label', title: '监测因子', className: 'cell-nowrap' },
    { key: 'period_label', title: '周期', className: 'cell-nowrap' },
    {
      key: 'value',
      title: '监测值',
      align: 'right',
      className: 'cell-nowrap',
      render: (row) => (
        <span style={{ color: !row.is_valid ? 'var(--warning)' : undefined }} className={row.is_exceeded && row.is_valid ? 'danger-text strong' : ''}>
          {formatNumber(row.value)} <span className="muted small">{row.unit}</span>
        </span>
      )
    },
    {
      key: 'limit_value',
      title: '限值',
      align: 'right',
      render: (row) => (row.limit_value === null ? <span className="muted small">无限值</span> : formatNumber(row.limit_value))
    },
    {
      key: 'is_exceeded',
      title: '判定',
      render: (row) => {
        if (!row.is_valid) return <Tag tone="warning">异常·不计入统计</Tag>
        return row.is_exceeded ? <Tag tone="danger">{formatRatio(row.exceed_ratio)}</Tag> : <Tag tone="success">达标</Tag>
      }
    },
    {
      key: 'data_source_label',
      title: '来源',
      render: (row) => <Tag tone={DATA_SOURCE_TONE[row.data_source]}>{row.data_source_label}</Tag>
    },
    { key: 'recorder', title: '录入人', render: (row) => row.recorder || '-' },
    {
      key: 'actions',
      title: '操作',
      align: 'right',
      render: (row) => (
        <div className="inline" style={{ gap: 6 }}>
          <button type="button" className="btn btn-sm" onClick={() => setCorrecting(row)}>
            {row.is_valid ? '改值' : '修正'}
          </button>
          <button type="button" className="btn btn-sm btn-danger" onClick={() => onDelete(row)}>
            删除
          </button>
        </div>
      )
    }
  ]

  return (
    <>
      <DataTable
        columns={columns}
        rows={rows}
        loading={loading}
        emptyText="暂无监测数据, 请先在上方录入"
        emptyIcon="✍️"
      />
      <CorrectDialog row={correcting} onClose={() => setCorrecting(null)} onCorrect={onCorrect} />
    </>
  )
}
