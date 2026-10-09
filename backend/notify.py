"""Native macOS notifications for incoming messages, from any channel.

A click brings the app to the front and asks the frontend (over WebSocket)
to open the conversation the notification was about.
"""

import logging
from typing import Optional

from backend.ws.manager import ws_manager

logger = logging.getLogger(__name__)

try:
    import Foundation
    import objc

    _NS_AVAILABLE = True
except ImportError:
    _NS_AVAILABLE = False


_webview_window = None  # set by main.py after webview.create_window()


def set_webview_window(window):
    """Register the pywebview window so notification clicks can navigate."""
    global _webview_window
    _webview_window = window


def _handle_notification_click(chat_id):
    """Handle notification click: bring app to front and navigate to chat via WebSocket."""
    try:
        from AppKit import NSApplication
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    except Exception:
        pass

    # Clicks arrive on the Cocoa main thread: hand the broadcast to the
    # server loop that owns the sockets.
    if chat_id:
        ws_manager.broadcast_threadsafe({"type": "navigate_to_chat", "chat_id": int(chat_id)})


if _NS_AVAILABLE:
    NSUserNotificationCenter = objc.lookUpClass("NSUserNotificationCenter")
    NSUserNotification = objc.lookUpClass("NSUserNotification")

    class _NotificationDelegate(Foundation.NSObject):
        """Delegate that forces notification display and handles clicks."""

        def userNotificationCenter_shouldPresentNotification_(
            self, center, notification
        ):
            return True

        def userNotificationCenter_didActivateNotification_(
            self, center, notification
        ):
            user_info = notification.userInfo()
            chat_id = user_info.get("chat_id") if user_info else None
            _handle_notification_click(chat_id)

    _delegate = _NotificationDelegate.alloc().init()
    _center = NSUserNotificationCenter.defaultUserNotificationCenter()
    if _center is not None:
        _center.setDelegate_(_delegate)


def send_native_notification(
    title: str, body: str, chat_id: Optional[int] = None
) -> None:
    """Send a macOS native notification attributed to the Mingly app."""
    if _NS_AVAILABLE:
        try:
            n = NSUserNotification.alloc().init()
            n.setTitle_(title)
            n.setInformativeText_(body)
            if chat_id is not None:
                n.setUserInfo_({"chat_id": chat_id})
            NSUserNotificationCenter.defaultUserNotificationCenter().deliverNotification_(n)
            return
        except Exception:
            pass
    logger.debug("Native notification unavailable, skipping")
