#!/bin/bash
# Railway multi-service startup router.
# desirable-insight  → Flask webhook server (fomo_tracker.py)
# crypto-strategy-clock → swing trade cron (crypto_oracle_v3.py --once)
echo "RAILWAY_SERVICE_NAME=$RAILWAY_SERVICE_NAME"
if [ "$RAILWAY_SERVICE_NAME" = "desirable-insight" ]; then
    echo "Starting FOMO Golem Flask server..."
    python fomo_tracker.py
elif [ "$RAILWAY_SERVICE_NAME" = "kalshi-strategy-clock" ]; then
    echo "Starting Kalshi Perps Tracker..."
    python kalshi_tracker.py
else
    echo "Starting Crypto Strategy Clock cron..."
    python crypto_oracle_v3.py --once
fi
