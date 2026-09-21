import { useMemo, useRef, useState } from 'react'
import { importEntries } from '../../../api/measurements.js'
import { SectionCard } from '../../../components/common/Card.jsx'
import { Alert } from '../../../components/common/Feedback.jsx'
import Tag from '../../../components/common/Tag.jsx'
import { useToast } from '../../../components/common/ToastProvider.jsx'
import { usePollutantMeta, useStationOptions } from '../../../hooks/useOptions.js'
import { useValuePolicy } from '../../../hooks/useValuePolicy.js'
import { validateReading } from '../../../utils/validation.js'

const PERIODS = [
  { value: 'hourly', label: '小时均值' },
  { value: 'daily', label: '日均值' }
]

const POLLUTANT_ALIASES = {
  PM25: 'PM25', 'PM2.5': 'PM25', '细颗粒物': 'PM25',
  PM10: 'PM10', '可吸入颗粒物': 'PM10',
  SO2: 'SO2', 'SO₂': 'SO2', '二氧化硫': 'SO2',
  NO2: 'NO2', 'NO₂': 'NO2', '二氧化氮': 'NO2',
  CO: 'CO', '一氧化碳': 'CO',
  O3: 'O3', 'O₃': 'O3', '臭氧': 'O3'
}

function splitLine(line) {
  // 兼容制表符粘贴 (Excel) 与逗号 CSV
  return line.includes('\t') ? line.split('\t') : line.split(/[,，]/)
}

function normalizeTime(raw) {
  const text = String(raw || '').trim().replace(/\//g, '-').replace('T', ' ')
  // yyyy-mm-dd HH:MM[:SS] -> yyyy-mm-dd HH:MM
  const match = text.match(/^(\d{4}-\d{1,2}-\d{1,2})(?:\s+(\d{1,2}:\d{1,2})(?::\d{1,2})?)?$/)
  if (!match) return null
  const [, datePart, timePart] = match
  const [year, month, day] = datePart.split('-').map(Number)
  const pad = (value) => String(value).padStart(2, '0')
  return `${year}-${pad(month)}-${pad(day)} ${timePart || '00:00'}`
}

/**
 * Parse pasted / file rows into import groups.
 * Columns: 监测点编码, 监测时间, 周期, 因子, 监测值 [, 录入人]
 */
export function parseRows(text, stationCodeToId) {
  const lines = String(text || '')
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)

  const issues = []
  const groupsByKey = new Map()
  let dataLines = 0

  lines.forEach((line, index) => {
    const lineNo = index + 1
    if (/监测点|站点编码|监测时间|因子|pollutant/i.test(line) && lineNo === 1) return // 表头
    const cells = splitLine(line).map((cell) => cell.trim())
    if (cells.length < 4) {
      issues.push({ line: lineNo, message: '列数不足, 应为: 站点编码, 监测时间, 周期, 因子, 监测值' })
      return
    }
    const [code, timeRaw, periodRaw, pollutantRaw, valueRaw, recorder] = cells
    const stationId = stationCodeToId[code]
    if (!stationId) {
      issues.push({ line: lineNo, message: `未知监测点编码: ${code}` })
      return
    }
    const measuredAt = normalizeTime(timeRaw)
    if (!measuredAt) {
      issues.push({ line: lineNo, message: `监测时间无法识别: ${timeRaw}` })
      return
    }
    const period = periodRaw.includes('日') || periodRaw.toLowerCase() === 'daily' ? 'daily' : 'hourly'
    const pollutant = POLLUTANT_ALIASES[pollutantRaw.toUpperCase()] || POLLUTANT_ALIASES[pollutantRaw]
    if (!pollutant) {
      issues.push({ line: lineNo, message: `未知监测因子: ${pollutantRaw}` })
      return
    }
    const number = Number(valueRaw)
    if (valueRaw === '' || Number.isNaN(number)) {
      issues.push({ line: lineNo, message: `监测值不是数字: ${valueRaw}` })
      return
    }
    dataLines += 1
    const key = `${stationId}|${measuredAt}|${period}`
    if (!groupsByKey.has(key)) {
      groupsByKey.set(key, {
        station_id: stationId,
        measured_at: measuredAt,
        period,
        data_source: 'import',
        recorder: recorder || undefined,
        entries: []
      })
    }
    groupsByKey.get(key).entries.push({ pollutant, value: number })
  })

  // 同组内因子重复
  for (const group of groupsByKey.values()) {
    const seen = new Set()
    for (const entry of group.entries) {
      if (seen.has(entry.pollutant)) {
        issues.push({ line: null, message: `${group.measured_at} 的 ${entry.pollutant} 重复出现` })
      }
      seen.add(entry.pollutant)
    }
  }

  return { groups: [...groupsByKey.values()], issues, rowCount: dataLines }
}

export default function BatchImportPanel({ onImported }) {
  const toast = useToast()
  const { data: stationData } = useStationOptions()
  const { data: pollutantData } = usePollutantMeta()
  const { policy } = useValuePolicy()
  const fileInputRef = useRef(null)

  const [text, setText] = useState('')
  const [overwrite, setOverwrite] = useState(false)
  const [busy, setBusy] = useState(false)
  const [serverError, setServerError] = useState(null)

  const codeToId = useMemo(() => {
    const map = {}
    ;(stationData?.items ?? []).forEach((station) => {
      map[station.code] = station.id
    })
    return map
  }, [stationData])

  const parsed = useMemo(() => parseRows(text, codeToId), [text, codeToId])

  // 页面端按服务端下发口径前置标红; 真正的拦截仍在服务端。
  const clientIssues = useMemo(() => {
    const list = []
    parsed.groups.forEach((group) => {
      group.entries.forEach((entry) => {
        const message = validateReading(entry.value, entry.pollutant, policy)
        if (message) {
          list.push(`${group.measured_at} ${entry.pollutant}=${entry.value}: ${message}`)
        }
      })
    })
    return list
  }, [parsed, policy])

  const handleFile = (event) => {
    const file = event.target.files?.[0]
    if (!file) return
    const reader = new FileReader()
    reader.onload = () => setText(String(reader.result || ''))
    reader.onerror = () => toast.error('文件读取失败')
    reader.readAsText(file, 'utf-8')
  }

  const submit = async () => {
    setServerError(null)
    if (parsed.issues.length || clientIssues.length) {
      toast.error('粘贴内容仍有未修正的问题, 请先处理标红行')
      return
    }
    if (!parsed.groups.length) {
      toast.error('没有可导入的数据行')
      return
    }
    setBusy(true)
    try {
      const result = await importEntries({ groups: parsed.groups, overwrite })
      toast.success(
        `导入完成: 新增 ${result.summary.created_count} 条, 更新 ${result.summary.updated_count} 条, 超标 ${result.summary.exceeded_count} 条`
      )
      setText('')
      if (fileInputRef.current) fileInputRef.current.value = ''
      onImported?.(result)
    } catch (error) {
      setServerError(error)
      toast.error(error.message)
    } finally {
      setBusy(false)
    }
  }

  const issueText = (issue) => (issue.line ? `第 ${issue.line} 行: ${issue.message}` : issue.message)

  return (
    <SectionCard
      title="批量粘贴 / 文件导入"
      hint="每行一条: 站点编码, 监测时间, 周期, 因子, 监测值。服务端会做同样的合理性校验, 任一行非法整批拒绝"
    >
      <div className="stack">
        <Alert tone="info">
          支持从 Excel 直接粘贴 (制表符分隔) 或选择 CSV 文件; 范围规则与手工录入完全一致,
          不能通过导入绕过负数 / 超量程拦截。
        </Alert>

        <div className="inline">
          <input ref={fileInputRef} type="file" accept=".csv,.txt,text/csv,text/plain" onChange={handleFile} />
          <label className="small muted inline" style={{ gap: 6 }}>
            <input type="checkbox" checked={overwrite} onChange={(event) => setOverwrite(event.target.checked)} />
            覆盖同一时刻已有数据
          </label>
        </div>

        <textarea
          className="input"
          rows={8}
          style={{ fontFamily: 'monospace' }}
          placeholder={'SZ-AQ-001,2026-09-14 10:00,小时均值,PM2.5,82.5\nSZ-AQ-001,2026-09-14 10:00,小时均值,SO2,640'}
          value={text}
          onChange={(event) => setText(event.target.value)}
        />

        <div className="small muted">
          可识别因子: {(pollutantData?.items ?? []).map((item) => item.label).join(' / ')}
        </div>

        {parsed.issues.length > 0 ? (
          <Alert tone="error">
            <div>{parsed.issues.length} 行无法解析:</div>
            <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
              {parsed.issues.slice(0, 8).map((issue, index) => (
                <li key={index}>{issueText(issue)}</li>
              ))}
            </ul>
          </Alert>
        ) : null}

        {clientIssues.length > 0 ? (
          <Alert tone="error">
            <div>{clientIssues.length} 条数值超出合理范围:</div>
            <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
              {clientIssues.slice(0, 8).map((item, index) => (
                <li key={index}>{item}</li>
              ))}
            </ul>
          </Alert>
        ) : null}

        {serverError ? (
          <Alert tone="error">
            {serverError.message}
            {Object.keys(serverError.fields || {}).length > 0 ? (
              <div className="small" style={{ marginTop: 4 }}>
                {Object.entries(serverError.fields)
                  .slice(0, 5)
                  .map(([key, code]) => `${key}: ${code}`)
                  .join('; ')}
              </div>
            ) : null}
          </Alert>
        ) : null}

        <div className="inline">
          <Tag tone="neutral">{parsed.groups.length} 组</Tag>
          <Tag tone="primary">{parsed.rowCount} 条数据</Tag>
          <button
            type="button"
            className="btn btn-primary"
            onClick={submit}
            disabled={busy || parsed.issues.length > 0 || clientIssues.length > 0 || parsed.groups.length === 0}
          >
            {busy ? '导入中...' : '提交导入 (服务端统一校验)'}
          </button>
        </div>
      </div>
    </SectionCard>
  )
}
