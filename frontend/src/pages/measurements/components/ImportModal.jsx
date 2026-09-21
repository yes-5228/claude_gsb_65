import { useMemo, useRef, useState } from 'react'
import { importRows } from '../../../api/measurements.js'
import Modal from '../../../components/common/Modal.jsx'
import { Checkbox, Field, Input, Select, Textarea } from '../../../components/common/FormField.jsx'
import { Alert } from '../../../components/common/Feedback.jsx'
import { useToast } from '../../../components/common/ToastProvider.jsx'
import { usePollutantMeta, useStationOptions } from '../../../hooks/useOptions.js'

const PERIODS = [
  { value: 'hourly', label: '小时均值' },
  { value: 'daily', label: '日均值' }
]

// 列名候选 -> 标准字段
const HEADER_MAP = {
  站点编码: 'station_code',
  站点: 'station_code',
  station_code: 'station_code',
  code: 'station_code',
  站点id: 'station_id',
  station_id: 'station_id',
  监测时间: 'measured_at',
  时间: 'measured_at',
  measured_at: 'measured_at',
  日期: 'measured_at',
  周期: 'period',
  period: 'period',
  因子: 'pollutant',
  监测因子: 'pollutant',
  pollutant: 'pollutant',
  监测值: 'value',
  数值: 'value',
  value: 'value',
  录入人: 'recorder',
  recorder: 'recorder',
  备注: 'remark',
  remark: 'remark'
}

const POLLUTANT_ALIASES = {
  'PM2.5': 'PM25',
  'PM2_5': 'PM25',
  '细颗粒物': 'PM25',
  PM10: 'PM10',
  '可吸入颗粒物': 'PM10',
  SO2: 'SO2',
  'SO₂': 'SO2',
  '二氧化硫': 'SO2',
  NO2: 'NO2',
  'NO₂': 'NO2',
  '二氧化氮': 'NO2',
  CO: 'CO',
  '一氧化碳': 'CO',
  O3: 'O3',
  'O₃': 'O3',
  '臭氧': 'O3'
}

function splitLine(line) {
  // 支持制表符 (Excel 粘贴) 与逗号 (CSV)
  return line.includes('\t') ? line.split('\t') : line.split(',')
}

/**
 * 把粘贴文本/CSV 解析为后端 rows。
 * 带表头时按列名映射; 无表头时按固定顺序:
 * 站点编码, 监测时间, 监测因子, 监测值[, 周期, 录入人, 备注]
 */
function parseText(text) {
  const lines = text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
  if (!lines.length) return { rows: [], error: '内容为空' }

  const firstCells = splitLine(lines[0]).map((cell) => cell.trim())
  const hasHeader = firstCells.some((cell) => HEADER_MAP[cell])

  if (hasHeader) {
    const keys = firstCells.map((cell) => HEADER_MAP[cell] || null)
    const rows = []
    for (const line of lines.slice(1)) {
      const cells = splitLine(line).map((cell) => cell.trim())
      const row = {}
      cells.forEach((cell, index) => {
        const key = keys[index]
        if (key) row[key] = cell
      })
      rows.push(row)
    }
    return { rows, header: keys }
  }

  const rows = lines.map((line) => {
    const cells = splitLine(line).map((cell) => cell.trim())
    return {
      station_code: cells[0],
      measured_at: cells[1],
      pollutant: POLLUTANT_ALIASES[cells[2]] || cells[2],
      value: cells[3],
      period: cells[4] || undefined,
      recorder: cells[5] || undefined,
      remark: cells[6] || undefined
    }
  })
  return { rows }
}

export default function ImportModal({ open, onClose, onImported }) {
  const toast = useToast()
  const { data: stationData } = useStationOptions()
  const { data: pollutantData } = usePollutantMeta()
  const fileRef = useRef(null)

  const [text, setText] = useState('')
  const [period, setPeriod] = useState('hourly')
  const [recorder, setRecorder] = useState('')
  const [overwrite, setOverwrite] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [parseError, setParseError] = useState(null)

  const pollutants = pollutantData?.items ?? []
  const stations = stationData?.items ?? []

  const preview = useMemo(() => {
    if (!text.trim()) return { rows: [], validCount: 0, issues: [] }
    const parsed = parseText(text)
    if (parsed.error) return { rows: [], validCount: 0, issues: [`解析失败: ${parsed.error}`] }
    const issues = []
    parsed.rows.forEach((row, index) => {
      const no = index + 1
      if (!row.station_code && !row.station_id) {
        issues.push(`第 ${no} 行: 缺少监测点`)
        return
      }
      const code = String(row.pollutant || '').trim()
      const normalized = POLLUTANT_ALIASES[code] || code.toUpperCase()
      const meta = pollutants.find((item) => item.code === normalized)
      if (!meta) {
        issues.push(`第 ${no} 行: 未知监测因子 "${row.pollutant}"`)
        return
      }
      if (!row.measured_at) {
        issues.push(`第 ${no} 行: 缺少监测时间`)
        return
      }
      const value = Number(row.value)
      if (row.value === '' || Number.isNaN(value) || !Number.isFinite(value)) {
        issues.push(`第 ${no} 行: ${meta.label} 监测值必须是有限数字`)
      } else if (value < 0) {
        issues.push(`第 ${no} 行: ${meta.label} 监测值不能为负数`)
      } else if (value > meta.value_max) {
        issues.push(`第 ${no} 行: ${meta.label} 监测值超出合理范围(0 ~ ${meta.value_max})`)
      }
    })
    return { rows: parsed.rows, validCount: parsed.rows.length - issues.length, issues }
  }, [text, pollutants])

  const reset = () => {
    setText('')
    setError(null)
    setParseError(null)
    setOverwrite(false)
    setRecorder('')
    setPeriod('hourly')
    if (fileRef.current) fileRef.current.value = ''
  }

  const handleClose = () => {
    reset()
    onClose?.()
  }

  const handleFile = async (event) => {
    const file = event.target.files?.[0]
    if (!file) return
    try {
      setText(await file.text())
      setParseError(null)
    } catch {
      setParseError('文件读取失败, 请改用粘贴方式')
    }
  }

  const submit = async () => {
    if (!preview.rows.length) {
      setError('请先粘贴或选择要导入的数据')
      return
    }
    if (preview.issues.length) {
      setError(`本地预检发现 ${preview.issues.length} 处问题, 请修正后再提交 (服务端也会整批拦截)`)
      return
    }
    setBusy(true)
    setError(null)
    try {
      const rows = preview.rows.map((row) => {
        const code = String(row.pollutant || '').trim()
        return {
          ...row,
          pollutant: POLLUTANT_ALIASES[code] || code.toUpperCase(),
          value: Number(row.value)
        }
      })
      const result = await importRows({
        rows,
        period,
        recorder: recorder || null,
        overwrite
      })
      toast.success(
        `导入完成: ${result.summary.group_count} 组共 ${result.summary.row_count} 条, ` +
          `新增 ${result.summary.created_count} / 更新 ${result.summary.updated_count}, ` +
          `超标 ${result.summary.exceeded_count} 条`
      )
      onImported?.(result)
      handleClose()
    } catch (err) {
      const fields = err.fields || {}
      const detail = Object.entries(fields)
        .slice(0, 8)
        .map(([key, message]) => `${key}: ${message}`)
        .join('；')
      setError(detail ? `${err.message}：${detail}` : err.message)
      toast.error(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={open}
      title="批量粘贴 / CSV 导入"
      width="wide"
      onClose={handleClose}
      footer={
        <>
          <button type="button" className="btn" onClick={handleClose} disabled={busy}>
            取消
          </button>
          <button type="button" className="btn btn-primary" onClick={submit} disabled={busy}>
            {busy ? '导入中...' : `导入 ${preview.rows.length || ''} 行`}
          </button>
        </>
      }
    >
      <div className="stack">
        <Alert tone="info">
          支持从 Excel 直接粘贴 (制表符分隔) 或选择 CSV 文件。服务端会对每一行执行与手工录入完全相同的校验:
          任一行为负数、非数字或超出合理量程, 整批拒绝, 不写入任何记录, 也不影响统计。
        </Alert>

        <div className="form-grid">
          <Field label="默认数据周期" hint="行内未填周期时使用">
            <Select value={period} onChange={(e) => setPeriod(e.target.value)} options={PERIODS} />
          </Field>
          <Field label="导入人">
            <Input value={recorder} onChange={(e) => setRecorder(e.target.value)} placeholder="如: 李四" />
          </Field>
          <Field label="CSV 文件" hint="也可以直接把表格内容粘贴到下方">
            <input ref={fileRef} type="file" accept=".csv,text/csv,text/plain" onChange={handleFile} />
          </Field>
          <Field label="重复数据">
            <Checkbox
              label="覆盖同一站点/时刻/因子已有数据"
              checked={overwrite}
              onChange={(e) => setOverwrite(e.target.checked)}
            />
          </Field>
        </div>

        <Field
          label="数据内容"
          hint={
            stations.length
              ? `无表头时按顺序: 站点编码, 监测时间, 监测因子, 监测值[, 周期, 录入人, 备注]。可用站点: ${stations
                  .slice(0, 4)
                  .map((s) => s.code)
                  .join('、')}...`
              : '无表头时按顺序: 站点编码, 监测时间, 监测因子, 监测值[, 周期, 录入人, 备注]'
          }
        >
          <Textarea
            rows={9}
            style={{ fontFamily: 'monospace' }}
            placeholder={'SZ-AQ-001\t2026-09-20 08:00\tPM2.5\t42.5\nSZ-AQ-001\t2026-09-20 08:00\tSO₂\t600'}
            value={text}
            onChange={(e) => {
              setText(e.target.value)
              setError(null)
            }}
          />
        </Field>

        {parseError ? <Alert tone="error">{parseError}</Alert> : null}
        {error ? <Alert tone="error">{error}</Alert> : null}

        {preview.rows.length > 0 ? (
          <Alert tone={preview.issues.length ? 'error' : 'success'}>
            共解析 {preview.rows.length} 行, 本地预检通过 {preview.validCount} 行
            {preview.issues.length ? `, 问题 ${preview.issues.length} 处` : ''}
            {preview.issues.length ? `: ${preview.issues.slice(0, 5).join('；')}${preview.issues.length > 5 ? ' ...' : ''}` : ''}
          </Alert>
        ) : null}
      </div>
    </Modal>
  )
}
