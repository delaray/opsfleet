# Context
The files and documents in this repository represent a production ready high-level design (HLP)of a data analyst chat agent implemented using LangGraph, Google Gemini LLM and the Google BQ database (read-only data source) as well as a PostgreSQL DB for writing, reading and deleting user generated reports. The tree main user activities will involve running a possibly multi-turn data analysis query and task, generating and retrieving reports and thirdly deleting reports the user is authorized to delete.

# Task Overview
This task involves creating a new README.md file in the project root directory that is intented for end-users of the datachat agent rather than the developers who built it. End-users will be mildly technical and should be able to follow a sequence of command to issue in a terminal window. 

This new README.md file will contain instructions for the end user to achieve the following:
1. A brief expanation of what this systemis and what it allows the end user to accomplish
2. Setup and installation instructions for the user to install the softwre locally on his machine.
3. Detailed instructions specifying precise how to execute the three types of tasks defined above.
4. A summary of the three existing user roles they can assume.
5. An explanation of how authentication, security, validation and governance is handled in the project
6. An examples section illustrating with examples and/or scenarios the three types of tasks the user can perform.
7. Any other useful:necessary information you think is important to make a new user experience pleasant and easily manageable and productive.

# Task Guidelines and Suggestions
1. Feel free to break the README.md file into several markdown files stored in the docs directory and all linked fro, the main README.md file in the project home directory.
2. Feel free to add diagrams and tables if you feel they can simplify long textual explanations or instructions, but be sure to well document explain any tables and diagrams.
3. Make a special effort to make the instructions and documentation elegant and pretty while also mainting a professional aspect 
4. Don't be afraid to use colors and visual cues as needed