UPDATE "NiftyPrediction"
SET primary_strategy = 'RangeBreakoutPut_GlobalAllDisagree'
WHERE primary_strategy = 'RangeBreakout_GlobalAllDisagree';

UPDATE "NiftyPrediction"
SET watch_variant = 'RangeBreakoutPut_GlobalAllDisagree'
WHERE watch_variant = 'RangeBreakout_GlobalAllDisagree';

UPDATE "NiftyPrediction"
SET prior_watch_variant = 'RangeBreakoutPut_GlobalAllDisagree'
WHERE prior_watch_variant = 'RangeBreakout_GlobalAllDisagree';

UPDATE "NiftyPrediction"
SET confirming_variant = 'RangeBreakoutPut_GlobalAllDisagree'
WHERE confirming_variant = 'RangeBreakout_GlobalAllDisagree';
