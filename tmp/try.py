#%%
from pprint import pprint
import sqlassert

# %%

pprint(sqlassert.analyze("""
create view fred as (
select derf, user_id, sum(amount) as spent from orders group by 1
);
select * from fred /**unique**/ join fred using(derf, user_id);
"""))

# %%
