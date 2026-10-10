/** Briefing schedule vocabulary shared by Settings and the Briefing tab. */

export const WEEKDAYS = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'] as const

export const dayLabel = (day: string) => day.charAt(0).toUpperCase() + day.slice(1)
