/** Home dashboard. */

import { request } from '../lib/http'
import type { DashboardData } from './types'

export const getDashboard = (signal?: AbortSignal) =>
  request<DashboardData>('/api/dashboard', { signal })
