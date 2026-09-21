import http, { toParams } from './client.js'

export const listMeasurements = (params) => http.get('/measurements', { params: toParams(params) })
export const previewEntries = (payload) => http.post('/measurements/preview', payload)
export const createEntries = (payload) => http.post('/measurements/entries', payload)
export const importEntries = (payload) => http.post('/measurements/import', payload)
export const deleteMeasurement = (id) => http.delete(`/measurements/${id}`)
export const correctMeasurement = (id, payload) => http.patch(`/measurements/${id}/correct`, payload)
export const entryContext = () => http.get('/measurements/entry-context')
export const valuePolicy = () => http.get('/measurements/value-policy')
export const qualitySummary = (params) =>
  http.get('/measurements/quality-summary', { params: toParams(params) })
export const runQualityScan = (payload = {}) => http.post('/measurements/quality-scan', payload)
export const exportMeasurementsUrl = (params) =>
  `/measurements/export?${new URLSearchParams(toParams(params)).toString()}`
