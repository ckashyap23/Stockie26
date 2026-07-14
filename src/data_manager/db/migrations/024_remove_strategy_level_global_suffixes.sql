UPDATE "NiftyPrediction"
SET primary_strategy = regexp_replace(primary_strategy, '_Global(All|Asia)(Agree|Disagree)$', '')
WHERE primary_strategy ~ '_Global(All|Asia)(Agree|Disagree)$';

UPDATE "NiftyPrediction"
SET watch_variant = regexp_replace(watch_variant, '_Global(All|Asia)(Agree|Disagree)$', '')
WHERE watch_variant ~ '_Global(All|Asia)(Agree|Disagree)$';

UPDATE "NiftyPrediction"
SET prior_watch_variant = regexp_replace(prior_watch_variant, '_Global(All|Asia)(Agree|Disagree)$', '')
WHERE prior_watch_variant ~ '_Global(All|Asia)(Agree|Disagree)$';

UPDATE "NiftyPrediction"
SET confirming_variant = regexp_replace(confirming_variant, '_Global(All|Asia)(Agree|Disagree)$', '')
WHERE confirming_variant ~ '_Global(All|Asia)(Agree|Disagree)$';

UPDATE "NiftyPrediction"
SET primary_strategy = 'RangeBreakoutPut'
WHERE primary_strategy = 'RangeBreakout';

UPDATE "NiftyPrediction"
SET watch_variant = 'RangeBreakoutPut'
WHERE watch_variant = 'RangeBreakout';

UPDATE "NiftyPrediction"
SET prior_watch_variant = 'RangeBreakoutPut'
WHERE prior_watch_variant = 'RangeBreakout';

UPDATE "NiftyPrediction"
SET confirming_variant = 'RangeBreakoutPut'
WHERE confirming_variant = 'RangeBreakout';
