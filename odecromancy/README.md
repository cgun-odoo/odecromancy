# Odecromancy (WIP)
**Imperfectly finds unused code in an Odoo project.**


### What's unused?
**Fields:** A field is not used if and only if it's:
1. Not used in any xml view.
2. Not used in any xml data (server actions and ir crons for now)
3. Not used in any method as the right side of an assignment.

**Methods:** A method is not used if an only if it's:
1. Not used in any xml view (From a button)
2. Not used in any other method


Somethings are missing for now and they are indicated with TODOs
- Parsing JS files
- Parsing reports
- ORM methods are never marked as unused. If the field of a compute method is unused it can be removed with the field.
- **confidence** is mentioned in some places in the code but it's not really used yet. 

Somethings will always be missing:
- Finding the return type of a method to mark subsequent field usages. 
Example: `model1.foo().field2` if `foo()` returns model2, model2.field2 is used but we have no way of knowing *yet*.
- If a view record is never displayed anywhere (Out of scope for now)
