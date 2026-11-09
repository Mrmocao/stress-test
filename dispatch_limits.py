"""Reserve extraction attempts before launching isolated workers."""


def validate_limits(ip_limit, failure_limit):
    for value, maximum, label in ((ip_limit, 400000, 'IP 提取上限'),
                                   (failure_limit, 1000, '连续失败暂停阈值')):
        if type(value) is not int or not 0 <= value <= maximum:
            raise ValueError(f'{label}必须为 0～{maximum} 的整数（0 表示关闭）')


class DispatchLimits:
    def __init__(self, relay, ip_limit=0, failure_limit=5):
        validate_limits(ip_limit, failure_limit)
        self.relay = relay
        self.ip_limit, self.failure_limit = ip_limit, failure_limit
        self.spent = self.reserved = self.failures = 0
        self.exhausted = False
        self.auto_paused = False

    def reserve(self, wanted):
        with self.relay.condition:
            while True:
                if self.exhausted:
                    return None
                if wanted and self.ip_limit and self.spent >= self.ip_limit and not self.reserved:
                    self.exhausted = True
                    self.relay.publish({'kind': 'quota', 'lane': 0, 'exhausted': True,
                                        'limit': self.ip_limit, 'charged_attempts': self.spent,
                                        'message': '已达到本批 IP 提取上限，当前轮完成后结束；未启动轮次取消'})
                    self.relay.condition.notify_all()
                    return None
                if self.relay.paused and (not self.relay.debug_controls or self.auto_paused):
                    self.relay.condition.wait()
                    continue
                if not wanted or not self.ip_limit:
                    return wanted
                available = self.ip_limit - self.spent - self.reserved
                if available > 0:
                    allocated = min(wanted, available)
                    self.reserved += allocated
                    return allocated
                if self.reserved:
                    self.relay.condition.wait()
                    continue

    def finish(self, allocated, attempted, reliable, outcome):
        with self.relay.condition:
            if self.ip_limit and allocated:
                self.reserved -= allocated
                # Unexpected process exit can lose buffered attempt events; keep
                # all reserved credits charged rather than overshooting the cap.
                self.spent += min(allocated, attempted) if reliable else allocated
            self.failures = self.failures + 1 if outcome == 'failed' else 0
            if self.failure_limit and self.failures >= self.failure_limit and not self.relay.paused:
                self.relay.paused = True
                self.auto_paused = True
                self.relay.publish({'kind': 'dispatch', 'lane': 0, 'paused': True,
                                    'automatic': True, 'consecutive_failures': self.failures,
                                    'message': f'连续 {self.failures} 轮失败，已自动暂停派发；当前轮继续完成，检查后点击继续'})
            self.relay.condition.notify_all()

    def resume(self):
        self.failures = 0
        self.auto_paused = False
