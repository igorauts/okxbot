# Engine 0.7.72 four-change candidate (OFFLINE)

- BUY: 20ms pacing only between successful BUYs; no final sleep.
- BUY/TP elective cancel quiet window: 12s.
- Virtual TP coalescing: 100ms.
- Account positions shared FIFO: 9 permits per 2.05s.
- Fills history quota unchanged. No emergency-path changes.
- Deployment requires coordinated transition of EVERY active quota consumer.
- Not a LIVE installer. No release publication.
