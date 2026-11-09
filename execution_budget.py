"""Monotonic lease budget shared by read queries and the decoder."""
import time

SAFETY_SECONDS = 2
STARTUP_SECONDS = 3
FINISH_SECONDS = 5


def minimum_lease_seconds(target):
    return target + SAFETY_SECONDS + STARTUP_SECONDS + FINISH_SECONDS


class BudgetExceeded(RuntimeError):
    pass


class ExecutionBudget:
    def __init__(self, expires_at, target_seconds, *, clock=time.monotonic):
        self.clock = clock
        self.deadline = expires_at - SAFETY_SECONDS
        self.target_seconds = target_seconds
        self.finish_reserve = FINISH_SECONDS
        self.startup_reserve = STARTUP_SECONDS
        self.preparing = True
        self.finishing = False

    def remaining(self):
        return max(0, self.deadline - self.clock())

    def request_remaining(self):
        reserve = (self.target_seconds + self.finish_reserve + self.startup_reserve
                   if self.preparing else (0 if self.finishing else self.finish_reserve))
        return max(0, self.remaining() - reserve)

    def timeout(self, maximum=5):
        available = self.request_remaining()
        if available < 0.1:
            raise BudgetExceeded('代理剩余寿命不足，无法完成本轮播放及收尾')
        return min(float(maximum), available)

    def can_retry(self, delay):
        return self.request_remaining() >= delay + 1

    def begin_playback(self, target):
        self.target_seconds = target
        if self.remaining() < target + self.finish_reserve + self.startup_reserve:
            raise BudgetExceeded('代理剩余寿命不足以完整播放目标时长及收尾，本轮跳过')
        self.preparing = False

    def check(self):
        if self.remaining() <= 0:
            raise BudgetExceeded('本轮代理时间预算已耗尽')

    def begin_finishing(self):
        self.preparing = False
        self.finishing = True
