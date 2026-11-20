"""Hidden Teams web view and the "Windows Screen" preview window (Cocoa side of kvm_viewer)."""

import sys
import time

import AppKit
import Foundation
import WebKit
from PyObjCTools import AppHelper

import kvm_viewer as kv

TICK = 1.5  # seconds between join steps / preview refreshes
WEB_SIZE = (1280, 800)
CAPTURE_TIMEOUT = 15


def log(message):
    """One line in the app log (~/Library/Logs/Mac2Win KVM.log)."""
    print(f"{time.strftime('%H:%M:%S')} viewer: {message}", file=sys.stderr, flush=True)


class WebDelegate(AppKit.NSObject):
    """Denies mic and camera, keeps Teams' pop-ups in the same hidden view, and logs
    navigation failures. ``viewer`` is set by Viewer (WKWebView only holds a weak reference)."""

    viewer = None

    def webView_requestMediaCapturePermissionForOrigin_initiatedByFrame_type_decisionHandler_(
            self, web_view, origin, frame, kind, handler):
        handler(WebKit.WKPermissionDecisionDeny)

    def webView_createWebViewWithConfiguration_forNavigationAction_windowFeatures_(
            self, web_view, configuration, action, features):
        url = action.request().URL()
        if url is not None and (url.host() or "").lower() == "teams.microsoft.com":
            web_view.loadRequest_(action.request())
        return None

    # WKNavigationDelegate

    def webView_didFinishNavigation_(self, web_view, navigation):
        url = web_view.URL()
        log(f"loaded {url.host() if url else None}{url.path() if url else ''}")

    def webView_didFailNavigation_withError_(self, web_view, navigation, error):
        log(f"navigation failed: {error.localizedDescription()} ({error.domain()} {error.code()})")

    def webView_didFailProvisionalNavigation_withError_(self, web_view, navigation, error):
        log(f"page load failed: {error.localizedDescription()} ({error.domain()} {error.code()})")

    def webViewWebContentProcessDidTerminate_(self, web_view):
        log("the web content process quit; reloading")
        if self.viewer is not None:
            self.viewer.reload("web content process quit")


class Viewer:
    """Joins the meeting in a hidden web view and shows the share in a preview window.

    All methods run on the main thread. ``on_change(message, level)`` reports status.
    """

    def __init__(self, on_change):
        self.on_change = on_change
        self.web = self.web_window = self.preview = self.image_view = self.status = None
        self.state = "stopped"
        self.running = False
        self.tick_pending = False
        self.delegate = WebDelegate.alloc().init()
        self.delegate.viewer = self
        self.url = None
        self.stall = kv.StallWatch()
        # Set by the app: whether something besides the preview wants frames, and who gets them.
        self.want_frames = lambda: False
        self.on_frame = None

    # ----- lifecycle -----

    def start(self, meeting_url):
        """Join ``meeting_url`` (raises ViewerError/BridgeError for a bad link)."""
        url = kv.viewer_url(meeting_url)
        if self.running:
            return
        config = WebKit.WKWebViewConfiguration.alloc().init()
        config.setWebsiteDataStore_(WebKit.WKWebsiteDataStore.nonPersistentDataStore())
        config.setMediaTypesRequiringUserActionForPlayback_(WebKit.WKAudiovisualMediaTypeNone)
        no_media = WebKit.WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(
            kv.NO_MEDIA_SCRIPT, WebKit.WKUserScriptInjectionTimeAtDocumentStart, False)
        config.userContentController().addUserScript_(no_media)
        errors = WebKit.WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(
            kv.ERROR_LOG_SCRIPT, WebKit.WKUserScriptInjectionTimeAtDocumentStart, True)
        config.userContentController().addUserScript_(errors)
        rect = Foundation.NSMakeRect(0, 0, *WEB_SIZE)
        self.web = WebKit.WKWebView.alloc().initWithFrame_configuration_(rect, config)
        # The window is transparent, which macOS reports as occluded; WebKit would then
        # throttle the page and Teams stops delivering the share video. Opt out (SPI).
        if self.web.respondsToSelector_("_setWindowOcclusionDetectionEnabled:"):
            self.web._setWindowOcclusionDetectionEnabled_(False)
        self.web.setCustomUserAgent_(kv.USER_AGENT)
        self.web.setUIDelegate_(self.delegate)
        self.web.setNavigationDelegate_(self.delegate)
        # Invisible but on screen: fully transparent, click-through, behind normal
        # windows. WebKit marks an off-screen page hidden (Teams may stop its video) and
        # throttles a borderless window, and macOS pulls a titled one back on screen anyway.
        self.web_window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, AppKit.NSWindowStyleMaskTitled, AppKit.NSBackingStoreBuffered, False)
        self.web_window.setReleasedWhenClosed_(False)
        self.web_window.setExcludedFromWindowsMenu_(True)
        self.web_window.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorTransient | AppKit.NSWindowCollectionBehaviorIgnoresCycle)
        self.web_window.setContentView_(self.web)
        self.web_window.setAlphaValue_(0.0)
        self.web_window.setIgnoresMouseEvents_(True)
        self.web_window.setLevel_(AppKit.NSNormalWindowLevel - 1)
        self.web_window.setFrameOrigin_((0, 0))
        self.web_window.orderBack_(None)
        self.url = url
        self.stall = kv.StallWatch()
        log(f"starting; window at {tuple(self.web_window.frame().origin)}")
        self.load()
        self.running = True
        self.set_state("loading")
        self.schedule()

    def load(self):
        self.web.loadRequest_(Foundation.NSURLRequest.requestWithURL_(Foundation.NSURL.URLWithString_(self.url)))

    def reload(self, reason):
        if not self.running:
            return
        log(f"reloading the meeting page ({reason})")
        self.load()

    def stop(self, state="stopped"):
        if not self.running:
            return
        log(f"stopping ({state})")
        self.running = False
        self.web.stopLoading()
        self.web.loadHTMLString_baseURL_("", None)  # leaves the call
        self.web_window.orderOut_(None)
        self.web_window.setContentView_(None)
        self.web = self.web_window = None
        if self.image_view is not None:
            self.image_view.setImage_(None)
        self.set_state(state)

    # ----- status -----

    def set_state(self, state):
        if state == self.state:
            return
        self.state = state
        self.log_page(state)
        message, level = kv.describe_state(state)
        if self.status is not None:
            self.status.setStringValue_(message)
        self.on_change(message, level)

    def log_page(self, state, script=kv.PAGE_INFO_SCRIPT):
        if self.web is None:
            return log(state)
        self.web.evaluateJavaScript_completionHandler_(
            script, lambda info, error: log(f"{state}: {info or error}"))

    def in_meeting(self):
        return self.state in ("in-meeting", "live")

    # ----- polling -----

    def schedule(self):
        if self.running and not self.tick_pending:
            self.tick_pending = True
            AppHelper.callLater(TICK, self.tick)

    def tick(self):
        self.tick_pending = False
        if not self.running:
            return
        self.web.evaluateJavaScript_completionHandler_(kv.join_script(), self.joined)

    def joined(self, result, error):
        if not self.running:
            return
        state = result if isinstance(result, str) and error is None else "loading"
        if state == "in-meeting" and self.state == "live":
            state = "live"
        if state == "ended":
            return self.stop("ended")
        self.set_state(state)
        if self.stall.update(state, time.monotonic()):
            self.log_page(f"stuck {state} for {self.stall.limit}s", kv.STUCK_INFO_SCRIPT)
            self.reload(f"attempt {self.stall.reloads} of {self.stall.max_reloads}")
        if self.in_meeting() and (self.preview_visible() or self.want_frames()):
            self.capture(self.show_frame)
        else:
            self.schedule()

    def show_frame(self, frame, error):
        if frame is not None:
            self.set_state("live")
            image = AppKit.NSImage.alloc().initWithData_(Foundation.NSData.dataWithBytes_length_(
                frame.data, len(frame.data)))
            if self.image_view is not None:
                self.image_view.setImage_(image)
            if self.on_frame is not None:
                self.on_frame(image)
        elif self.state == "live":
            self.set_state("in-meeting")
        self.schedule()

    def capture(self, callback):
        """Grab the current share frame; calls ``callback(frame, error_message)``."""
        if not self.running:
            return callback(None, "The Windows screen viewer is not running.")
        if not self.in_meeting():
            return callback(None, kv.describe_state(self.state)[0])

        def done(result, error):
            try:
                if error is not None:
                    raise kv.ViewerError(f"The Teams viewer failed to read the screen: {error}")
                frame = kv.parse_frame(result)
            except kv.ViewerError as exc:
                if isinstance(result, str) and "no-video" in result:
                    occluded = not (self.web_window.occlusionState() & AppKit.NSWindowOcclusionStateVisible)
                    log(f"no video: {result[:400]} occluded={occluded}")
                return callback(None, str(exc))
            callback(frame, None)
        self.web.evaluateJavaScript_completionHandler_(kv.FRAME_SCRIPT, done)

    # ----- preview window -----

    def preview_visible(self):
        return self.preview is not None and self.preview.isVisible()

    def show_preview(self, delegate):
        if self.preview is None:
            self.build_preview(delegate)
        self.preview.makeKeyAndOrderFront_(None)
        self.schedule()

    def build_preview(self, delegate):
        style = (AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable
                 | AppKit.NSWindowStyleMaskResizable | AppKit.NSWindowStyleMaskMiniaturizable)
        window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            Foundation.NSMakeRect(0, 0, 864, 580), style, AppKit.NSBackingStoreBuffered, False)
        window.setTitle_("Windows Screen")
        window.setReleasedWhenClosed_(False)
        window.setContentMinSize_((360, 260))
        window.setFrameAutosaveName_("WindowsScreen")
        content = AppKit.NSView.alloc().init()
        self.image_view = AppKit.NSImageView.alloc().init()
        self.image_view.setImageScaling_(AppKit.NSImageScaleProportionallyUpOrDown)
        self.image_view.setTranslatesAutoresizingMaskIntoConstraints_(False)
        self.status = AppKit.NSTextField.labelWithString_(kv.describe_state(self.state)[0])
        self.status.setTextColor_(AppKit.NSColor.secondaryLabelColor())
        self.status.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
        self.status.setTranslatesAutoresizingMaskIntoConstraints_(False)
        stop = AppKit.NSButton.buttonWithTitle_target_action_("Stop Viewer", delegate, "stopViewer:")
        stop.setTranslatesAutoresizingMaskIntoConstraints_(False)
        stop.setToolTip_(f"{kv.VIEWER_NAME} leaves the Teams meeting")
        for view in (self.image_view, self.status, stop):
            content.addSubview_(view)
        constraints = [
            self.image_view.topAnchor().constraintEqualToAnchor_constant_(content.topAnchor(), 8),
            self.image_view.leadingAnchor().constraintEqualToAnchor_constant_(content.leadingAnchor(), 8),
            self.image_view.trailingAnchor().constraintEqualToAnchor_constant_(content.trailingAnchor(), -8),
            self.image_view.bottomAnchor().constraintEqualToAnchor_constant_(stop.topAnchor(), -8),
            stop.trailingAnchor().constraintEqualToAnchor_constant_(content.trailingAnchor(), -12),
            stop.bottomAnchor().constraintEqualToAnchor_constant_(content.bottomAnchor(), -10),
            self.status.leadingAnchor().constraintEqualToAnchor_constant_(content.leadingAnchor(), 12),
            self.status.trailingAnchor().constraintLessThanOrEqualToAnchor_constant_(stop.leadingAnchor(), -8),
            self.status.centerYAnchor().constraintEqualToAnchor_(stop.centerYAnchor()),
        ]
        AppKit.NSLayoutConstraint.activateConstraints_(constraints)
        self.status.setContentCompressionResistancePriority_forOrientation_(
            250, AppKit.NSLayoutConstraintOrientationHorizontal)
        window.setContentView_(content)
        window.center()
        self.preview = window
