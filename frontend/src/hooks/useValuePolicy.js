import { useCallback } from 'react'
import { valuePolicy as fetchValuePolicy } from '../api/measurements.js'
import { useAsyncData } from './useAsyncData.js'
import { FALLBACK_POLICY } from '../utils/validation.js'

let policyCache = null

export function useValuePolicy() {
  const loader = useCallback(async () => {
    if (!policyCache) {
      policyCache = fetchValuePolicy().catch((error) => {
        policyCache = null
        throw error
      })
    }
    return policyCache
  }, [])
  const { data, loading, error } = useAsyncData(loader)
  return { policy: data ?? FALLBACK_POLICY, loading, error }
}
