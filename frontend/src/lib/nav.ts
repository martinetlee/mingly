/**
 * Sidebar navigation — the single source for the Sidebar links, the ⌘1-9
 * shortcuts (Layout) and the shortcut help sheet.
 */
import { Home, MessageSquare, Users, Building2, Radio, Shield, Brain, Settings, Lock } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

import type { Features } from '../api/types'

export interface NavItem {
  to: string
  icon: LucideIcon
  label: string
  /** Shown only when this backend feature is on */
  feature?: keyof Features
}

/** Every main nav item, top to bottom (unfiltered — use visibleNav) */
export const NAV: NavItem[] = [
  { to: '/home', icon: Home, label: 'Home' },
  { to: '/chats', icon: MessageSquare, label: 'Chats' },
  { to: '/contacts', icon: Users, label: 'Contacts' },
  { to: '/organizations', icon: Building2, label: 'Orgs' },
  { to: '/tracked', icon: Radio, label: 'Tracked' },
  { to: '/shield', icon: Shield, label: 'Shield', feature: 'shield' },
  { to: '/intelligence', icon: Brain, label: 'Intelligence' },
]

/** Pinned to the bottom of the sidebar */
export const SETTINGS_NAV: NavItem = { to: '/settings', icon: Settings, label: 'Settings' }

/** Main nav items visible with the given features */
export function visibleNav(features: Features): NavItem[] {
  return NAV.filter((item) => !item.feature || features[item.feature])
}

export interface NavShortcut { combo: string; keys: string; item: NavItem }

/** ⌘1…⌘9 targets, in sidebar order (visible main nav, then Settings) */
export function navShortcuts(features: Features): NavShortcut[] {
  return [...visibleNav(features), SETTINGS_NAV]
    .slice(0, 9)
    .map((item, i) => ({ combo: `mod+${i + 1}`, keys: `⌘${i + 1}`, item }))
}

/** Sidebar "Lock" action (Layout binds the shortcut, ShortcutHelp lists it) */
export const LOCK_ACTION = { combo: 'mod+shift+l', keys: '⌘⇧L', icon: Lock, label: 'Lock' } as const
