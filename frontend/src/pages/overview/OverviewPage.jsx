import { useCallback } from 'react'
import { Link } from 'react-router-dom'
import { overview } from '../../api/meta.js'
import BarChart from '../../components/common/BarChart.jsx'
import { SectionCard } from '../../components/common/Card.jsx'
import DataTable from '../../components/common/DataTable.jsx'
import { Alert, ErrorState, Loading } from '../../components/common/Feedback.jsx'
import StatCard from '../../components/common/StatCard.jsx'
import Tag from '../../components/common/Tag.jsx'
import { EXCEEDANCE_LEVEL_TONE } from '../../constants/index.js'
import { useAsyncData } from '../../hooks/useAsyncData.js'
import { formatDateTime, formatNumber, formatPercent, formatRatio } from '../../utils/format.js'

export default function OverviewPage() {
  const loader = useCallback(() => overview(), [])
  const { data, loading, error, reload } = useAsyncData(loader)

  if (loading && !data) return <Loading text="正在加载运行概览..." />
  if (error && !data) return <ErrorState error={error} onRetry={reload} />
  if (!data) return null

  const { stations, measurements, exceedances, trend, pending_exceedances: pending } = data

  const pendingColumns = [
    { key: 'measured_at', title: '监测时间', className: 'cell-nowrap', render: (row) => formatDateTime(row.measured_at) },
    { key: 'station_name', title: '监测点', render: (row) => row.station_name },
    { key: 'pollutant_label', title: '因子' },
    {
      key: 'value',
      title: '监测值 / 限值',
      render: (row) => `${formatNumber(row.value)} / ${formatNumber(row.limit_value)}`
    },
    { key: 'exceed_ratio', title: '超标倍数', render: (row) => formatRatio(row.exceed_ratio) },
    {
      key: 'level',
      title: '等级',
      render: (row) => <Tag tone={EXCEEDANCE_LEVEL_TONE[row.level]}>{row.level_label}</Tag>
    }
  ]

  const typeRows = (stations.by_type || []).map((item) => ({
    id: item.key,
    label: item.label,
    count: item.count,
    ratio: stations.total ? item.count / stations.total : 0
  }))

  return (
    <>
      {measurements.invalid_count > 0 ? (
        <Alert tone="warning">
          检测到 <strong>{measurements.invalid_count}</strong> 条历史异常数据(负值或超量程极大值),
          已从达标率与站点排名中剔除, 请前往
          <Link to="/measurements"> 监测数据录入 </Link>
          页修正; 修正后各项统计会按同一口径自动重算。超标工作台另有 {exceedances.invalid_count} 条异常关联单不计入统计。
        </Alert>
      ) : null}

      <div className="stat-grid">
        <StatCard
          label="监测点总数"
          value={stations.total}
          foot={(stations.by_status || [])
            .map((item) => `${item.label} ${item.count}`)
            .join(' · ')}
        />
        <StatCard
          label="有效监测数据"
          value={measurements.valid_count}
          tone={measurements.invalid_count > 0 ? 'warning' : undefined}
          foot={
            measurements.invalid_count > 0
              ? `共 ${measurements.total} 条, 其中异常 ${measurements.invalid_count} 条已剔除`
              : `覆盖 ${measurements.station_count} 个监测点 · 均值 ${formatNumber(measurements.avg_value)}`
          }
        />
        <StatCard
          label="达标率(有效口径)"
          value={formatPercent(measurements.compliance_rate)}
          tone={measurements.compliance_rate < 0.9 ? 'danger' : undefined}
          foot={`${measurements.valid_exceeded_count} 条超标 · 最大超标倍数 ${formatRatio(exceedances.max_ratio)}`}
        />
        <StatCard
          label="待标注超标"
          value={exceedances.pending}
          tone={exceedances.pending ? 'warning' : undefined}
          foot={
            <Link to="/exceedances">前往标注工作台 →</Link>
          }
        />
      </div>

      <div className="grid-2">
        <SectionCard title="近 7 日数据量趋势" hint="按日统计录入条数, 红色代表当日存在超标">
          <BarChart items={trend.items || []} precision={0} danger={false} />
        </SectionCard>

        <SectionCard title="监测点类型分布" hint="台账中各类站点的数量占比">
          <div className="stack">
            {typeRows.map((row) => (
              <div key={row.id}>
                <div className="inline" style={{ justifyContent: 'space-between' }}>
                  <span>{row.label}</span>
                  <span className="muted small">
                    {row.count} 个 · {formatPercent(row.ratio)}
                  </span>
                </div>
                <div className="meter" style={{ marginTop: 6 }}>
                  <div className="meter-fill" style={{ width: `${Math.max(row.ratio * 100, 2)}%` }} />
                </div>
              </div>
            ))}
            <div className="inline">
              <Link className="btn btn-sm" to="/stations">
                管理监测点台账
              </Link>
              <Link className="btn btn-sm" to="/measurements">
                录入监测数据
              </Link>
            </div>
          </div>
        </SectionCard>
      </div>

      <SectionCard
        title="待标注超标记录 (最近 5 条)"
        hint="按监测时间倒序, 点击“超标记录标注”模块可批量处理"
        actions={
          <Link className="btn btn-sm btn-primary" to="/exceedances">
            处理超标标注
          </Link>
        }
      >
        {exceedances.pending === 0 ? (
          <Alert tone="success">当前没有待标注的超标记录, 数据复核已完成 ✅</Alert>
        ) : (
          <DataTable
            columns={pendingColumns}
            rows={pending}
            loading={loading}
            emptyText="暂无待标注记录"
            emptyIcon="✅"
          />
        )}
      </SectionCard>
    </>
  )
}
