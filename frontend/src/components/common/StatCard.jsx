export default function StatCard({ label, value, unit, foot, tone }) {
  const toneColor = {
    danger: 'var(--danger)',
    warning: 'var(--warning)',
    success: 'var(--success)',
    info: 'var(--info, var(--primary))'
  }[tone]
  const color = toneColor || 'var(--text)'
  return (
    <div className="stat-card">
      <div className="stat-label">{label}</div>
      <div className="stat-value" style={{ color }}>
        {value}
        {unit ? <small>{unit}</small> : null}
      </div>
      {foot ? <div className="stat-foot">{foot}</div> : null}
    </div>
  )
}
