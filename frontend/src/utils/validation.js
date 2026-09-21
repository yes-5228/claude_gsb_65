// 页面端校验只是前置提示, 服务端 domain.value_gate 才是最终闸门。
// 范围由 /api/measurements/value-policy 下发, 避免前后端口径分叉。

export const FALLBACK_POLICY = {
  min_value: 0,
  max_value_absolute: 10000,
  max_value_by_pollutant: {
    PM25: 1000,
    PM10: 2000,
    SO2: 1000,
    NO2: 1000,
    CO: 100,
    O3: 1000
  }
}

export function maxFor(policy, pollutant) {
  return policy?.max_value_by_pollutant?.[pollutant] ?? policy?.max_value_absolute ?? 10000
}

/**
 * Validate a raw input for a pollutant.
 * Returns null when valid, otherwise a Chinese error message.
 */
export function validateReading(raw, pollutant, policy = FALLBACK_POLICY) {
  const text = String(raw ?? '').trim()
  if (text === '') return '监测值不能为空'
  const number = Number(text)
  if (Number.isNaN(number)) return '监测值必须是数字'
  if (!Number.isFinite(number)) return '监测值不能为 NaN 或无穷大'
  if (number < (policy?.min_value ?? 0)) return '监测值不能为负数'
  const upper = maxFor(policy, pollutant)
  if (number > upper) return `监测值超出可信量程(上限 ${upper}), 请检查单位或小数点`
  return null
}
