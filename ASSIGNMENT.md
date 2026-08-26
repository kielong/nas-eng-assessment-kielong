### NAS Backend Coding Challenge

#### Objective

Implement a simple [FastAPI](https://fastapi.tiangolo.com) backend to decode VINs, powered by the [vPIC API](https://vpic.nhtsa.dot.gov/api/) and backed by a [SQLite](https://www.sqlite.org/index.html) cache.

#### Time

Take as much time as you need. We aren't timing you, and we'd rather see something you're comfortable talking through than something rushed.

#### On Using AI

Using AI coding assistants (Claude, Copilot, Cursor, ChatGPT, etc.) is completely fine — use whatever tools you'd use on a normal workday.

What matters is that you can stand behind what you submit. When we review this together, we're going to spend most of our time talking through the code: why you structured it the way you did, what the tradeoffs were, what you'd change if this had to handle real traffic, and where the weak spots are. If your assistant made a decision you didn't think about, that will show up quickly in the conversation.

Put differently: **the conversation about the assignment matters more to us than the finished artifact.** A modest implementation you understand deeply will do better here than a polished one you can't explain.

#### Requirements

Your application should contain three (3) routes:

`/lookup`

This route will first check the SQLite database to see if a cached result is available. If so, it should be returned
from the database.

If not, your API should contact the vPIC API to decode the VIN, store the results in the database, and return the
result.

The request should contain a single string called "vin". It should contain exactly 17 alphanumeric characters.

The response object should contain the following elements:

- Input VIN Requested (string, exactly 17 alphanumeric characters)
- Make (String)
- Model (String)
- Model Year (String)
- Body Class (String)
- Cached Result? (Boolean)

`/remove`

This route will remove a entry from the cache.

The request should contain a single string called "vin". It should contain exactly 17 alphanumeric characters.

The response object should contain the following elements:

- Input VIN Requested (string, exactly 17 alphanumeric characters)
- Cache Delete Success? (boolean)

`/export`

This route will export the SQLite database cache and return a binary file (parquet format) containing the data in the
cache.

No additional input/data should be required to make the request.

The response object should be a binary file downloaded by the client containing all currently
cached VINs in a table stored in parquet format.

#### Build Setup and Deploy

Use FastAPI as your web framework. You may structure your project as you wish.

You do not need to deploy your code, but you should be prepared to have a conversation about how to do so.

Include instructions in your README for getting the service running locally from a clean checkout.

Also include a short `NOTES.md` alongside your code. What goes in it is up to you — a page or less of whatever you think is worth telling us about what you built.

#### Submitting Your Work

When you're ready, push your work to a GitHub repo and send us the link.

The repo needs to be either public, or private and shared with the GitHub user `atkincaid74`. If you were given different submission instructions along with this challenge, follow those instead.

#### Our Evaluation

- Basic functionality
- Code quality
- Error handling
- Documentation (readme/comments/tests)
- Ability to explain your implementation decisions

Please feel free to be creative and add any embellishments or additional functionality you would like to show off!

#### Test VINs

You may use the following test VINs. We encourage you to try VINs you may find from other sources!

`1HGCM82633A004352`
`5YJ3E1EA6PF384836`
`1FTFW1ET9DFC10312`
`1C4RJFBG2FC625797`
`5FNRL6H79NB021411`
`1HD1KBM15FB620271`
`1XPWD40X1ED215307`
