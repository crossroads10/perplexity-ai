import { BrutalistCard } from './ui/BrutalistCard'
import { PoolStatus, HeartbeatConfig } from 'lib/api'

interface StatsGridProps {
  data: PoolStatus
  hbConfig: HeartbeatConfig | null
  hasLoaded?: boolean
}

export function StatsGrid({
  data,
  hbConfig,
  hasLoaded = true
}: StatsGridProps) {
  const getAutoRenewStatus = () => {
    if (!hbConfig) return { label: '—', color: 'text-gray-500' }
    if (!hbConfig.enable) return { label: 'Off', color: 'text-gray-500' }
    return { label: 'On', color: 'text-green-500' }
  }

  const hbStatus = getAutoRenewStatus()

  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-4 md:gap-6 mb-8">
      <BrutalistCard
        number="01"
        label="Total accounts"
        value={hasLoaded ? data.total : '—'}
        colorClass="text-acid"
      />
      <BrutalistCard
        number="02"
        label="Available"
        value={hasLoaded ? data.available : '—'}
        colorClass="text-green-400"
      />
      <BrutalistCard
        number="03"
        label="Unavailable"
        value={hasLoaded ? Math.max(0, data.total - data.available) : '—'}
        colorClass="text-yellow-400"
      />
      <BrutalistCard
        number="04"
        label="Auto renewal"
        value={hbStatus.label}
        colorClass={hbStatus.color}
      />
    </div>
  )
}
