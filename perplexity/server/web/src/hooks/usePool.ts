import { useState, useEffect, useCallback, useRef } from 'react'
import {
  fetchPoolStatus,
  fetchHeartbeatConfig,
  fetchFallbackConfig,
  fetchIncognitoConfig,
  PoolStatus,
  HeartbeatConfig,
  FallbackConfig,
  IncognitoConfig
} from 'lib/api'

export function usePool(adminToken: string) {
  const [data, setData] = useState<PoolStatus>({
    total: 0,
    available: 0,
    mode: '-',
    clients: []
  })
  const [hbConfig, setHbConfig] = useState<HeartbeatConfig | null>(null)
  const [fallbackConfig, setFallbackConfig] = useState<FallbackConfig>({
    fallback_to_auto: true
  })
  const [incognitoConfig, setIncognitoConfig] = useState<IncognitoConfig>({
    enabled: false
  })
  const [settingsReady, setSettingsReady] = useState(false)
  const [isLoading, setIsLoading] = useState(true)
  const [lastSync, setLastSync] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [configError, setConfigError] = useState<string | null>(null)
  const activeRequest = useRef<AbortController | null>(null)

  const refreshData = useCallback(async () => {
    activeRequest.current?.abort()
    const controller = new AbortController()
    activeRequest.current = controller
    const timeout = window.setTimeout(() => controller.abort(), 15000)
    setIsLoading(true)
    try {
      const [pool, fallback, incognito, heartbeat] = await Promise.allSettled([
        fetchPoolStatus(controller.signal),
        fetchFallbackConfig(controller.signal),
        fetchIncognitoConfig(controller.signal),
        adminToken
          ? fetchHeartbeatConfig(adminToken, controller.signal)
          : Promise.resolve(null)
      ])
      // A login, logout or newer refresh supersedes the previous response.
      if (activeRequest.current !== controller) return
      if (pool.status === 'fulfilled') {
        setData(pool.value)
        setLastSync(new Date().toLocaleTimeString([], { hour12: false }))
        setError(null)
      } else {
        setError(
          'Could not refresh account status. Check the connection and try again.'
        )
      }
      const fallbackOk =
        fallback.status === 'fulfilled' &&
        fallback.value?.status === 'ok' &&
        !!fallback.value.config
      const incognitoOk =
        incognito.status === 'fulfilled' &&
        incognito.value?.status === 'ok' &&
        !!incognito.value.config
      const heartbeatOk =
        !adminToken ||
        (heartbeat.status === 'fulfilled' &&
          heartbeat.value?.status === 'ok' &&
          !!heartbeat.value.config)
      if (fallbackOk) setFallbackConfig(fallback.value.config!)
      if (incognitoOk) setIncognitoConfig(incognito.value.config!)
      setHbConfig(
        heartbeat.status === 'fulfilled' && heartbeat.value?.status === 'ok'
          ? heartbeat.value.config || null
          : null
      )
      setSettingsReady(fallbackOk && incognitoOk)
      setConfigError(
        fallbackOk && incognitoOk && heartbeatOk
          ? null
          : 'Some settings could not be loaded. Refresh to retry; if this persists, sign in again.'
      )
    } finally {
      window.clearTimeout(timeout)
      if (activeRequest.current === controller) {
        activeRequest.current = null
        setIsLoading(false)
      }
    }
  }, [adminToken])

  useEffect(() => {
    setHbConfig(null)
    void refreshData()
    const interval = window.setInterval(() => {
      if (!activeRequest.current) void refreshData()
    }, 30000)
    return () => {
      window.clearInterval(interval)
      activeRequest.current?.abort()
      activeRequest.current = null
    }
  }, [refreshData])

  return {
    data,
    hbConfig,
    setHbConfig,
    fallbackConfig,
    setFallbackConfig,
    incognitoConfig,
    setIncognitoConfig,
    settingsReady,
    isLoading,
    lastSync,
    error,
    configError,
    refreshData
  }
}
