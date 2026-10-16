"""Exchange rates: a currency's rates on a day, from Frankfurter (free, no key), cached in
MongoDB so each currency and day is fetched once (today's again after FX_CACHE_TTL_HOURS)."""
