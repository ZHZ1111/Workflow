# Controlled execution timeout

`calculate_total(12, 3)` should return 36. This fixture deliberately loops forever
to verify the execution timeout and process termination. It must never be used
as a passing example or silently repaired by changing the original input.
