/** Translation. */

import { request } from '../lib/http'

export const translateMessage = (text: string, targetLanguage: string) =>
  request<{ translation: string }>('/api/translate', {
    method: 'POST',
    body: JSON.stringify({ text, target_language: targetLanguage }),
  })

export const setTranslationLanguage = (language: string) =>
  request<{ success: boolean; language: string }>('/api/settings/prefs/translation-language', {
    method: 'POST',
    body: JSON.stringify({ language }),
  })
