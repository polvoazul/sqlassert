create table users (id integer primary key, email varchar);
create table user_emails (user_id integer, email varchar);

select u.id, e.email
from users u
/**ASSERT MANY TO ONE**/ join user_emails e on e.user_id = u.id;
