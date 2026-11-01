# Overview
The POC implementation currently uses SQLite for storing relational.

# Task 
Replace the SQLite implementation with postgreSQL and update all queries and ruses of SQLite.
You should create a new DB and call it **datachatdb**.
In addition to an system admin role (let's call it admindb and just give it the default PG pwd **postgres**), you should additionally create basic user roles for the three executives user-a, user-b and user-c so that their reports will have the additional security and authorisation provided by PG.

# Guidelines
1.This machine alrwady has PG installed and configured with several of my own databases.
2. Make sure youy create a brand new database called **datachatdb** and role for accessing this new database.
3. I have added my current PG credentials to the .env file which you can use to perform the neessary operations, but create new credential for the new database so that my current ones remain only for my existing databases and PG installation. I will be dockerizing this project later in a separate task and would like the new DB credentials to be used in the dockerized version without needing my existing credentials

# Final Test
Once you are done you should run a basic workflow test and a basic report generation test for a particular user and store the report in PG.