@echo off
REM PC37 Critical Acceptance Test Runner
REM Run all acceptance tests and generate report

echo Starting PC37 Critical Acceptance Tests...
echo ============================================

REM Run the main harness
python "C:\projects\OryaObservability\scripts\acceptance\pc37_harness.py"

echo.
echo ============================================
echo Test completed. Check docs/critical-acceptance.md for results.

pause